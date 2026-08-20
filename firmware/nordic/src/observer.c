#include "observer.h"

#include <errno.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/logging/log.h>

#include "report.h"
#include "air_wire.h"

LOG_MODULE_REGISTER(Module_Observer, LOG_LEVEL_INF);

/* What the scanner saw. Lifetime totals, not reset by a run, and logged in full
 * by observer_stop(). A delivery ratio is not trustworthy without them: "heard no
 * neighbour data" and "heard plenty, none of it ours" look identical from the
 * agent's side.
 *
 * Private, and logged rather than reported: getting them to the host needs a
 * STATS payload decision the host has not made -- its STATS_FIELDS is a fixed
 * 12-field peer-specific layout these do not map onto. See PLATFORM.md 8b.A2. */
static struct {
	uint32_t devices;       /* advertising reports handed to the parser */
	uint32_t ours;          /* our company id and a valid netid byte */
	uint32_t foreign;       /* manufacturer element, but not ours */
	uint32_t wrong_size;    /* ours, but no version's length */
	uint32_t malformed;     /* right length, unreadable content */
	uint32_t legacy_v0;     /* a board still transmitting the 6-byte payload */
	uint32_t unknown_node;  /* ours, but not a declared neighbour */
	uint32_t queue_drops;   /* a pending snapshot overwritten before it was read */
} counters;

static neighbor_info_type      neighbor_info;
static struct agent           *agent;       /* read-only; bound by observer_bind() */
static bool                    scanning;

/* Depth 1: only the newest snapshot is of any use, and a queue that buffers old
 * ones hands the control law stale neighbour values. */
K_MSGQ_DEFINE(custom_observer_msg_queue, sizeof(neighbor_info_type), 1, 4);

/* Current scan configuration. Defaults are the values that were hardcoded here,
 * so behaviour is unchanged until the host sends a RADIO frame. */
static uint16_t scan_interval = BT_GAP_SCAN_FAST_INTERVAL;
static uint16_t scan_window   = BT_GAP_SCAN_FAST_WINDOW;
static bool     scan_active;

void observer_bind(struct agent *a)
{
	agent = a;
}

/**
 * Private callback: one AD element of a received advertisement.
 *
 * Returns false to stop walking the remaining elements once ours is found, true
 * to keep looking. Runs in the Bluetooth RX thread, so it does no HCI calls, no
 * logging above LOG_DBG, and no writes to `struct agent`.
 */
static bool on_data_parse_after_device_found(struct bt_data *data, void *user_data)
{
	state_packet_type *pkt = user_data;

	if (data->type != BT_DATA_MANUFACTURER_DATA) {
		return true;
	}
	if (agent == NULL) {
		return false;       /* scanning before observer_bind(): nowhere to map to */
	}

	/* air_wire_decode_any, not a v1-only decoder: it mirrors the host's decode_any()
	 * so a bench with some boards reflashed and some not degrades to missing
	 * timestamps rather than a silent blackout. Counted separately either way. */
	const int rc = air_wire_decode_any(data->data, data->data_len, pkt);
	if (rc == AIR_WIRE_ERR_COMPANY) {
		counters.foreign++;
		return false;
	}
	if (rc == AIR_WIRE_ERR_LEN) {
		counters.wrong_size++;
		return false;
	}
	if (rc != AIR_WIRE_OK) {
		counters.malformed++;
		return false;
	}
	counters.ours++;
	if (!pkt->has_seq_and_time) {
		/* A board still running the old 6-byte payload. Its vstate is usable and
		 * is used; its delay and per-sequence loss are not derivable. Counted so
		 * a half-reflashed fleet is visible in the log rather than showing up as
		 * one link mysteriously missing its delay figures. */
		counters.legacy_v0++;
	}

	const int8_t node_index = agent_neighbor_index(agent, pkt->node);
	if (node_index < 0) {
		/* One of ours, but not a declared neighbour of this node. Expected on a
		 * shared bench; worth counting so an unexpected topology is visible. */
		counters.unknown_node++;
		return false;
	}

	neighbor_info.vstates[node_index] = pkt->vstate;
	neighbor_info.enabled[node_index] = pkt->enabled;
	neighbor_info.seq[node_index]     = pkt->seq;
	neighbor_info.heard |= (1u << (unsigned)node_index);

	/* Before the queue put, so overwriting a pending snapshot cannot lose the
	 * evidence that this link delivered. `fresh` is what makes per-link delivery
	 * ratio derivable from the log alone. */
	report_mark_fresh((uint8_t)node_index);

	/* k_msgq has no overwrite primitive, so purge and retry. The purge discards a
	 * snapshot the consumer had not read yet -- which is the intent at depth 1,
	 * but it is a dropped observation and gets counted as one. */
	while (k_msgq_put(&custom_observer_msg_queue, &neighbor_info, K_NO_WAIT) != 0) {
		counters.queue_drops++;
		k_msgq_purge(&custom_observer_msg_queue);
	}

	/* LOG_DBG, not LOG_INF: four neighbours at a 100 ms advertising interval is
	 * ~40 lines a second, over the same UART that carries the STATE reports.
	 * Saturating it is what voided loopback direction A's delivery ratio. */
	LOG_DBG("node %u seq %u vstate %d enabled %d",
		pkt->node, pkt->seq, pkt->vstate, (int)pkt->enabled);
	return false;
}

/**
 * Private callback: one advertising report.
 *
 * RSSI is recorded because it is the only liveness signal that survives a
 * neighbour going quiet: a vstate that stops changing and a vstate that stopped
 * arriving look the same otherwise.
 */
static void on_device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			    struct net_buf_simple *ad)
{
	ARG_UNUSED(addr);
	ARG_UNUSED(type);

	state_packet_type pkt = {0};
	const uint32_t before = counters.ours;

	counters.devices++;
	bt_data_parse(ad, on_data_parse_after_device_found, &pkt);

	if (counters.ours != before && agent != NULL) {
		const int8_t idx = agent_neighbor_index(agent, pkt.node);
		if (idx >= 0) {
			neighbor_info.rssi[idx] = rssi;
		}
	}
}

int observer_init(void)
{
	if (scanning) {
		return 0;
	}
	struct bt_le_scan_param scan_param = {
		.type     = scan_active ? BT_LE_SCAN_TYPE_ACTIVE : BT_LE_SCAN_TYPE_PASSIVE,
		/* Duplicate filtering ON, as it has always been here. The Pi-side scanner
		 * deliberately turns it off, because a suppressed duplicate is
		 * indistinguishable from a lost packet -- the number being measured. The
		 * two receive paths are therefore measuring loss under different rules.
		 * Left as-is rather than changed quietly: flipping it changes what every
		 * `ble` agent has recorded. See PLATFORM.md 8b.A0. */
		.options  = BT_LE_SCAN_OPT_FILTER_DUPLICATE,
		.interval = scan_interval,
		.window   = scan_window,
	};

	int err = bt_le_scan_start(&scan_param, on_device_found);
	if (err) {
		LOG_ERR("Scanning failed to start (err %d)", err);
		return err;
	}
	scanning = true;
	LOG_INF("Scanning started: interval %u window %u (%u%% duty) %s",
		scan_interval, scan_window,
		(unsigned)(100u * scan_window / scan_interval),
		scan_active ? "active" : "passive");
	return 0;
}

int observer_set_scan_params(uint16_t interval, uint16_t window, bool active)
{
	if (interval == 0u || window > interval) {
		return -EINVAL;
	}
	scan_interval = interval;
	scan_window   = window;
	scan_active   = active;

	if (!scanning) {
		/* Stored for the next observer_init(). Deliberately not started here: a
		 * RADIO frame arrives before CONTROL, and switching the receiver on
		 * during configuration would change the run's airtime baseline. */
		LOG_INF("scan params stored: interval %u window %u %s",
			interval, window, active ? "active" : "passive");
		return 0;
	}

	/* Mid-run change: restart so it takes effect now. Zephyr passes these through
	 * bt_le_scan_start(), so there is no way to apply them without stopping. */
	int err = bt_le_scan_stop();
	if (err) {
		LOG_ERR("Scanning failed to stop for reconfiguration (err %d)", err);
		return err;
	}
	scanning = false;
	return observer_init();
}

int observer_stop(void)
{
	if (!scanning) {
		return 0;
	}
	int err = bt_le_scan_stop();
	if (err) {
		LOG_ERR("Scanning failed to stop (err %d)", err);
		return err;
	}
	scanning = false;
	k_msgq_purge(&custom_observer_msg_queue);

	/* Once per run, not once per packet. This is the first question when a
	 * delivery ratio comes out at zero: "the receiver heard nothing" and "the
	 * receiver heard plenty, none of it ours" look identical from the agent's
	 * side, and only these numbers separate them. */
	LOG_INF("Scan totals: devices %u ours %u (v0 %u) foreign %u wrong_size %u "
		"malformed %u unknown_node %u queue_drops %u",
		counters.devices, counters.ours, counters.legacy_v0, counters.foreign,
		counters.wrong_size, counters.malformed, counters.unknown_node,
		counters.queue_drops);

	/* Forget the run: `heard` is per-run, and carrying it into the next one would
	 * report a neighbour as observed on the strength of the previous run. The
	 * counters are deliberately NOT reset -- they are lifetime totals, and the
	 * host reads them before and after a run and subtracts. */
	memset(&neighbor_info, 0, sizeof(neighbor_info));
	return 0;
}
