"""
Tests for the refactored PrefixListMgr and the new SUPPRESS_PREFIX type.

This file implements the test cases described in
docs/testplan/PrefixListMgr-Refactor-Test-Plan.md. TC-A2 is intentionally
omitted here because it is covered by re-running the existing
tests/bgp/test_prefix_list.py on the candidate image.

Per-test topology marks are applied because the same file covers
ANCHOR_PREFIX (spine-only) and SUPPRESS_PREFIX (any device) scenarios.
"""
import logging
import random
import time

import pytest
import yaml

from tests.common.config_reload import config_reload
from tests.common.helpers.assertions import pytest_assert, pytest_require
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

CONSTANTS_FILE = "/etc/sonic/constants.yml"
SUPPRESS_TYPE = "SUPPRESS_PREFIX"
ANCHOR_TYPE = "ANCHOR_PREFIX"
DEFAULT_SUPPRESS_V4_NAME = "SUPPRESS_IPV4_PREFIX"
DEFAULT_SUPPRESS_V6_NAME = "SUPPRESS_IPV6_PREFIX"

SUPPRESS_PREFIXES = {
    "ipv4": ["192.168.100.0/24", "192.168.101.0/24"],
    "ipv6": ["2001:db8:abcd::/48", "2001:db8:abce::/48"],
}

ANCHOR_TEST_V4 = "205.168.0.0/24"
ANCHOR_TEST_V6 = "50c0::/48"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def op_prefix_with_cmd(duthost, prefix_type, prefix, action, ignore_error=False):
    """Run `sudo prefix_list <action> <type> <prefix>` on the DUT."""
    pytest_assert(action in ("add", "remove", "status"),
                  "Invalid action: {}".format(action))
    cmd = "sudo prefix_list {} {} {}".format(action, prefix_type, prefix).strip()
    return duthost.shell(cmd, module_ignore_errors=ignore_error)


def get_device_metadata(duthost):
    """Return (type, subtype) strings from DEVICE_METADATA.localhost."""
    cfg = duthost.config_facts(host=duthost.hostname,
                               source="running")["ansible_facts"]
    md = cfg.get("DEVICE_METADATA", {}).get("localhost", {})
    return md.get("type", ""), md.get("subtype", "")


def is_spine_like(duthost):
    """True for UpstreamLC or UpperSpineRouter devices."""
    dtype, dsub = get_device_metadata(duthost)
    return dtype == "UpperSpineRouter" or (dtype == "SpineRouter"
                                           and dsub == "UpstreamLC")


def read_constants(duthost):
    """Load /etc/sonic/constants.yml as a Python dict (empty dict if missing)."""
    if not duthost.stat(path=CONSTANTS_FILE)["stat"]["exists"]:
        return {}
    return yaml.safe_load(
        duthost.shell("cat {}".format(CONSTANTS_FILE))["stdout"]) or {}


def get_suppress_pl_names(duthost):
    """Return (ipv4_name, ipv6_name) honoring constants.yml override."""
    constants = read_constants(duthost)
    pl = (constants.get("constants", {})
                   .get("bgp", {})
                   .get("prefix_list", {})
                   .get(SUPPRESS_TYPE, {}))
    return (pl.get("ipv4_name", DEFAULT_SUPPRESS_V4_NAME),
            pl.get("ipv6_name", DEFAULT_SUPPRESS_V6_NAME))


def _expected_status_occurrences(duthost):
    """Number of times each (type, prefix) tuple should appear in `status`."""
    asics = duthost.get_frontend_asic_ids() or []
    return len(asics) if asics else 1


def verify_prefix_list_in_db(duthost, prefix_type, prefix):
    """Return True iff `prefix_list status` reports the prefix on every asic."""
    out = duthost.shell("sudo prefix_list status")["stdout"]
    tag = "('{}', '{}')".format(prefix_type, prefix)
    expected = _expected_status_occurrences(duthost)
    count = out.count(tag)
    if count != expected:
        logger.info("status returned %d occurrences of %s, expected %d",
                    count, tag, expected)
    return count == expected


def _vtysh_show_prefix_list(duthost, name, ipv, asic=None):
    ns = "-n {}".format(asic) if (asic is not None and duthost.is_multi_asic) else ""
    cmd = 'vtysh {} -c "show {} prefix-list {}"'.format(ns, ipv, name)
    return duthost.shell(cmd, module_ignore_errors=True)["stdout"]


def verify_frr_prefix_list_entry(duthost, name, prefix, ipv, present=True):
    """ipv must be 'ip' or 'ipv6'. Verifies on every frontend asic."""
    asics = duthost.get_frontend_asic_ids() or [None]
    matches = []
    for asic in asics:
        out = _vtysh_show_prefix_list(duthost, name, ipv, asic=asic)
        matches.append(prefix in out)
    return all(matches) if present else not any(matches)


def _syslog_contains(duthost, pattern):
    cmd = "sudo grep -E '{}' /var/log/syslog | tail -50".format(pattern)
    return bool(duthost.shell(cmd,
                              module_ignore_errors=True)["stdout"].strip())


def _write_constants(duthost, data):
    """Render `data` as YAML and overwrite /etc/sonic/constants.yml."""
    duthost.copy(content=yaml.safe_dump(data, default_flow_style=False),
                 dest=CONSTANTS_FILE)


def _restart_bgp(duthost):
    duthost.shell("sudo docker restart bgp")
    pytest_assert(
        wait_until(180, 5, 10,
                   duthost.is_service_running, "bgpcfgd", "bgp"),
        "bgpcfgd did not come back after `docker restart bgp`",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rand_one_uplink_duthost(duthosts):
    """Pick one UpstreamLC linecard randomly (skip otherwise)."""
    uplinks = [d for d in duthosts
               if not d.is_supervisor_node() and is_spine_like(d)]
    if not uplinks:
        pytest.skip("No upstream linecard / UpperSpineRouter found")
    return random.choice(uplinks)


@pytest.fixture(scope="function")
def non_spine_duthost(duthosts, rand_one_dut_hostname):
    """Pick a non-spine, non-supervisor duthost from the testbed."""
    duthost = duthosts[rand_one_dut_hostname]
    pytest_require(not duthost.is_supervisor_node(),
                   "Test requires a frontend duthost, got supervisor")
    pytest_require(not is_spine_like(duthost),
                   "Test requires a non-spine duthost, got spine-like device")
    return duthost


@pytest.fixture(scope="function")
def cleanup_suppress(duthosts, rand_one_dut_hostname):
    """Yield a frontend duthost and clean up any SUPPRESS_PREFIX entries we
    might have left behind."""
    duthost = duthosts[rand_one_dut_hostname]
    pytest_require(not duthost.is_supervisor_node(),
                   "Test requires a frontend duthost, got supervisor")
    yield duthost
    for ipv in ("ipv4", "ipv6"):
        for pfx in SUPPRESS_PREFIXES[ipv]:
            op_prefix_with_cmd(duthost, SUPPRESS_TYPE, pfx,
                               "remove", ignore_error=True)


@pytest.fixture(scope="function")
def cleanup_suppress_on_uplink(rand_one_uplink_duthost):
    """Same as cleanup_suppress but pinned to the uplink linecard."""
    duthost = rand_one_uplink_duthost
    yield duthost
    for pfx in (ANCHOR_TEST_V4, ANCHOR_TEST_V6):
        op_prefix_with_cmd(duthost, ANCHOR_TYPE, pfx,
                           "remove", ignore_error=True)


@pytest.fixture(scope="function")
def restore_constants(duthosts, rand_one_dut_hostname):
    """Snapshot /etc/sonic/constants.yml and restore it (and restart bgp) on
    teardown."""
    duthost = duthosts[rand_one_dut_hostname]
    backup_path = "/tmp/constants.yml.bak.test_prefix_list_suppress"
    duthost.shell("sudo cp {} {}".format(CONSTANTS_FILE, backup_path))
    yield duthost
    duthost.shell("sudo cp {} {}".format(backup_path, CONSTANTS_FILE))
    duthost.shell("sudo rm -f {}".format(backup_path))
    _restart_bgp(duthost)


# ---------------------------------------------------------------------------
# TC-A1: ANCHOR_PREFIX CLI add/remove/status on UpstreamLC
# ---------------------------------------------------------------------------
@pytest.mark.topology("t2")
def test_anchor_prefix_cli_on_upstream_lc(cleanup_suppress_on_uplink):
    """TC-A1: ANCHOR_PREFIX add/remove/status round-trip on UpstreamLC."""
    duthost = cleanup_suppress_on_uplink

    op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V4, "add")
    op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V6, "add")

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_prefix_list_in_db,
                   duthost, ANCHOR_TYPE, ANCHOR_TEST_V4),
        "ANCHOR_PREFIX v4 not visible in `prefix_list status`",
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_prefix_list_in_db,
                   duthost, ANCHOR_TYPE, ANCHOR_TEST_V6),
        "ANCHOR_PREFIX v6 not visible in `prefix_list status`",
    )

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                   ANCHOR_TEST_V4, "ip", True),
        "ANCHOR_PREFIX v4 missing from ANCHOR_CONTRIBUTING_ROUTES",
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                   ANCHOR_TEST_V6, "ipv6", True),
        "ANCHOR_PREFIX v6 missing from ANCHOR_CONTRIBUTING_ROUTES",
    )

    op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V4, "remove")
    op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V6, "remove")

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                   ANCHOR_TEST_V4, "ip", False),
        "ANCHOR_PREFIX v4 still in ANCHOR_CONTRIBUTING_ROUTES after remove",
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                   ANCHOR_TEST_V6, "ipv6", False),
        "ANCHOR_PREFIX v6 still in ANCHOR_CONTRIBUTING_ROUTES after remove",
    )


# ---------------------------------------------------------------------------
# TC-A3: ANCHOR_PREFIX rejected on non-spine device
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1")
def test_anchor_prefix_rejected_on_non_spine(non_spine_duthost):
    """TC-A3: CLI rejects ANCHOR_PREFIX; direct DB write logs warning."""
    duthost = non_spine_duthost

    res = op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V4,
                             "add", ignore_error=True)
    pytest_assert(res["rc"] != 0,
                  "CLI must reject ANCHOR_PREFIX on non-spine, got rc=0")
    stderr = (res.get("stderr") or "") + " " + (res.get("stdout") or "")
    pytest_assert(
        "is not supported on device type" in stderr,
        "Stderr should explain device gating, got: {}".format(stderr),
    )

    # Bypass the CLI by writing directly to CONFIG_DB and make sure
    # PrefixListMgr logs the warning and FRR is not updated.
    db_key = "PREFIX_LIST|ANCHOR_PREFIX|{}".format(ANCHOR_TEST_V4)
    try:
        duthost.shell(
            'sonic-db-cli CONFIG_DB hset "{}" NULL NULL'.format(db_key))
        pytest_assert(
            wait_until(30, 2, 0,
                       _syslog_contains, duthost,
                       "not supported for ANCHOR_PREFIX"),
            "bgpcfgd should warn that ANCHOR_PREFIX is not allowed",
        )
        pytest_assert(
            verify_frr_prefix_list_entry(
                duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                ANCHOR_TEST_V4, "ip", present=False),
            "FRR should not pick up ANCHOR_PREFIX on a non-spine device",
        )
    finally:
        duthost.shell(
            'sonic-db-cli CONFIG_DB del "{}"'.format(db_key),
            module_ignore_errors=True)

    # bgpcfgd must still be healthy.
    pytest_assert(
        duthost.is_service_running("bgpcfgd", "bgp"),
        "bgpcfgd crashed after rejected ANCHOR_PREFIX write",
    )


# ---------------------------------------------------------------------------
# TC-A4: ANCHOR_PREFIX persists across `config reload` and bgp restart
# ---------------------------------------------------------------------------
@pytest.mark.topology("t2")
def test_anchor_prefix_persists(cleanup_suppress_on_uplink):
    """TC-A4: ANCHOR_PREFIX survives `config save && config reload` and
    `docker restart bgp`."""
    duthost = cleanup_suppress_on_uplink

    op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V4, "add")
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                   ANCHOR_TEST_V4, "ip", True),
        "ANCHOR_PREFIX v4 not seen in FRR after add",
    )

    duthost.shell("sudo config save -y")
    try:
        config_reload(duthost, safe_reload=True, check_intf_up_ports=True,
                      wait_for_bgp=True)
        pytest_assert(
            wait_until(120, 5, 10,
                       verify_frr_prefix_list_entry,
                       duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                       ANCHOR_TEST_V4, "ip", True),
            "ANCHOR_PREFIX v4 missing after `config reload`",
        )

        _restart_bgp(duthost)
        pytest_assert(
            wait_until(60, 5, 5,
                       verify_frr_prefix_list_entry,
                       duthost, "ANCHOR_CONTRIBUTING_ROUTES",
                       ANCHOR_TEST_V4, "ip", True),
            "ANCHOR_PREFIX v4 missing after `docker restart bgp`",
        )
    finally:
        op_prefix_with_cmd(duthost, ANCHOR_TYPE, ANCHOR_TEST_V4,
                           "remove", ignore_error=True)
        duthost.shell("sudo config save -y", module_ignore_errors=True)


# ---------------------------------------------------------------------------
# TC-A5: PrefixListMgr is registered (and healthy) on every device
# ---------------------------------------------------------------------------
@pytest.mark.topology("any")
def test_prefix_list_mgr_registered(duthosts, rand_one_dut_hostname):
    """TC-A5: bgpcfgd is RUNNING, no traceback, no stale `enabled for
    UpperSpineRouter/UpstreamLC` log line."""
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Supervisor cards do not run bgpcfgd")

    pytest_assert(
        duthost.is_service_running("bgpcfgd", "bgp"),
        "bgpcfgd is not running on {}".format(duthost.hostname),
    )

    # The old (pre-PR) message should no longer appear.
    legacy = duthost.shell(
        "sudo grep -E 'Prefix List Manager and AsPath Manager are enabled "
        "for (UpperSpineRouter|UpstreamLC)' /var/log/syslog | tail -5",
        module_ignore_errors=True)["stdout"]
    pytest_assert(
        not legacy.strip(),
        "Found stale legacy bgpcfgd log line: {}".format(legacy),
    )

    # No traceback in bgpcfgd output.
    tb = duthost.shell(
        "sudo grep -E 'Traceback|PrefixListMgr.*Exception' "
        "/var/log/syslog | tail -20",
        module_ignore_errors=True)["stdout"]
    pytest_assert(
        not tb.strip(),
        "PrefixListMgr produced tracebacks/exceptions: {}".format(tb),
    )


# ---------------------------------------------------------------------------
# TC-S1 + TC-S2: SUPPRESS_PREFIX CLI add/remove/status + FRR rendering
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1", "t2")
def test_suppress_prefix_cli_and_frr(cleanup_suppress):
    """TC-S1 + TC-S2: SUPPRESS_PREFIX add/remove for v4 + v6 reflected in
    CONFIG_DB and in FRR `<ip|ipv6> prefix-list <name>`."""
    duthost = cleanup_suppress
    ipv4_name, ipv6_name = get_suppress_pl_names(duthost)

    v4_pfx = SUPPRESS_PREFIXES["ipv4"][0]
    v6_pfx = SUPPRESS_PREFIXES["ipv6"][0]

    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4_pfx, "add")
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v6_pfx, "add")

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_prefix_list_in_db, duthost,
                   SUPPRESS_TYPE, v4_pfx),
        "v4 SUPPRESS_PREFIX not present in `prefix_list status`",
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_prefix_list_in_db, duthost,
                   SUPPRESS_TYPE, v6_pfx),
        "v6 SUPPRESS_PREFIX not present in `prefix_list status`",
    )

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv4_name, v4_pfx, "ip", True),
        "v4 prefix not found in FRR prefix-list {}".format(ipv4_name),
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv6_name, v6_pfx, "ipv6", True),
        "v6 prefix not found in FRR prefix-list {}".format(ipv6_name),
    )

    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4_pfx, "remove")
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v6_pfx, "remove")

    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv4_name, v4_pfx, "ip", False),
        "v4 prefix still in FRR after remove",
    )
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv6_name, v6_pfx, "ipv6", False),
        "v6 prefix still in FRR after remove",
    )


# ---------------------------------------------------------------------------
# TC-S3: SUPPRESS_PREFIX works on any device type (also via direct DB write)
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1", "t2")
def test_suppress_prefix_works_on_any_device(cleanup_suppress):
    """TC-S3: PrefixListMgr accepts SUPPRESS_PREFIX without device gating."""
    duthost = cleanup_suppress
    ipv4_name, _ = get_suppress_pl_names(duthost)
    v4_pfx = SUPPRESS_PREFIXES["ipv4"][1]

    # Path 1: via CLI.
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4_pfx, "add")
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv4_name, v4_pfx, "ip", True),
        "SUPPRESS_PREFIX should be accepted via CLI on any device",
    )
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4_pfx, "remove")

    # Path 2: direct CONFIG_DB write to confirm the manager itself does not
    # gate on device type.
    db_key = "PREFIX_LIST|{}|{}".format(SUPPRESS_TYPE, v4_pfx)
    try:
        duthost.shell(
            'sonic-db-cli CONFIG_DB hset "{}" NULL NULL'.format(db_key))
        pytest_assert(
            wait_until(30, 2, 0,
                       verify_frr_prefix_list_entry,
                       duthost, ipv4_name, v4_pfx, "ip", True),
            "SUPPRESS_PREFIX direct-DB write should reach FRR on any device",
        )
        # No "not supported for SUPPRESS_PREFIX" warning expected.
        pytest_assert(
            not _syslog_contains(duthost,
                                 "not supported for SUPPRESS_PREFIX"),
            "Unexpected device-gating warning for SUPPRESS_PREFIX",
        )
    finally:
        duthost.shell(
            'sonic-db-cli CONFIG_DB del "{}"'.format(db_key),
            module_ignore_errors=True)


# ---------------------------------------------------------------------------
# TC-S4: SUPPRESS_PREFIX name override via constants.yml
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1")
def test_suppress_prefix_constants_override(cleanup_suppress, restore_constants):
    """TC-S4: Custom names from constants.yml override the registry defaults."""
    duthost = cleanup_suppress
    pytest_require(duthost.hostname == restore_constants.hostname,
                   "cleanup_suppress and restore_constants picked "
                   "different DUTs; fixture chain is misconfigured")

    constants = read_constants(duthost) or {}
    constants.setdefault("constants", {}) \
             .setdefault("bgp", {}) \
             .setdefault("prefix_list", {})[SUPPRESS_TYPE] = {
                 "ipv4_name": "CUSTOM_IPV4_PREFIX",
                 "ipv6_name": "CUSTOM_IPV6_PREFIX",
             }
    _write_constants(duthost, constants)
    _restart_bgp(duthost)

    v4 = SUPPRESS_PREFIXES["ipv4"][0]
    v6 = SUPPRESS_PREFIXES["ipv6"][0]
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4, "add")
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v6, "add")

    pytest_assert(
        wait_until(60, 3, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "CUSTOM_IPV4_PREFIX", v4, "ip", True),
        "Override ipv4_name not applied in FRR",
    )
    pytest_assert(
        wait_until(60, 3, 0,
                   verify_frr_prefix_list_entry,
                   duthost, "CUSTOM_IPV6_PREFIX", v6, "ipv6", True),
        "Override ipv6_name not applied in FRR",
    )
    pytest_assert(
        verify_frr_prefix_list_entry(
            duthost, DEFAULT_SUPPRESS_V4_NAME, v4, "ip", present=False),
        "Default v4 name should not be present when override is set",
    )
    pytest_assert(
        verify_frr_prefix_list_entry(
            duthost, DEFAULT_SUPPRESS_V6_NAME, v6, "ipv6", present=False),
        "Default v6 name should not be present when override is set",
    )


# ---------------------------------------------------------------------------
# TC-S5: SUPPRESS_PREFIX falls back to registry defaults when no override
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1", "t2")
def test_suppress_prefix_constants_fallback(cleanup_suppress, restore_constants):
    """TC-S5: Without `bgp.prefix_list.SUPPRESS_PREFIX`, defaults are used."""
    duthost = cleanup_suppress
    pytest_require(duthost.hostname == restore_constants.hostname,
                   "cleanup_suppress and restore_constants picked "
                   "different DUTs; fixture chain is misconfigured")

    constants = read_constants(duthost) or {}
    # Drop the entire bgp.prefix_list block if present.
    bgp = constants.get("constants", {}).get("bgp", {})
    bgp.pop("prefix_list", None)
    _write_constants(duthost, constants)
    _restart_bgp(duthost)

    v4 = SUPPRESS_PREFIXES["ipv4"][0]
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4, "add")

    pytest_assert(
        wait_until(60, 3, 0,
                   verify_frr_prefix_list_entry,
                   duthost, DEFAULT_SUPPRESS_V4_NAME, v4, "ip", True),
        "Default name {} not applied when constants.yml override is absent"
        .format(DEFAULT_SUPPRESS_V4_NAME),
    )


# ---------------------------------------------------------------------------
# TC-S6: SUPPRESS_PREFIX persists across `config reload` and bgp restart
# ---------------------------------------------------------------------------
@pytest.mark.topology("t0", "t1")
def test_suppress_prefix_persists(cleanup_suppress):
    """TC-S6: SUPPRESS_PREFIX entries survive `config reload` + bgp restart."""
    duthost = cleanup_suppress
    ipv4_name, ipv6_name = get_suppress_pl_names(duthost)
    v4 = SUPPRESS_PREFIXES["ipv4"][0]
    v6 = SUPPRESS_PREFIXES["ipv6"][0]

    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4, "add")
    op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v6, "add")
    pytest_assert(
        wait_until(30, 2, 0,
                   verify_frr_prefix_list_entry,
                   duthost, ipv4_name, v4, "ip", True),
        "v4 SUPPRESS_PREFIX not in FRR after add",
    )

    duthost.shell("sudo config save -y")
    try:
        config_reload(duthost, safe_reload=True, check_intf_up_ports=True,
                      wait_for_bgp=True)
        pytest_assert(
            wait_until(120, 5, 10,
                       verify_frr_prefix_list_entry,
                       duthost, ipv4_name, v4, "ip", True),
            "v4 SUPPRESS_PREFIX missing after `config reload`",
        )
        pytest_assert(
            wait_until(60, 3, 0,
                       verify_frr_prefix_list_entry,
                       duthost, ipv6_name, v6, "ipv6", True),
            "v6 SUPPRESS_PREFIX missing after `config reload`",
        )

        _restart_bgp(duthost)
        pytest_assert(
            wait_until(60, 5, 5,
                       verify_frr_prefix_list_entry,
                       duthost, ipv4_name, v4, "ip", True),
            "v4 SUPPRESS_PREFIX missing after `docker restart bgp`",
        )
    finally:
        op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v4,
                           "remove", ignore_error=True)
        op_prefix_with_cmd(duthost, SUPPRESS_TYPE, v6,
                           "remove", ignore_error=True)
        duthost.shell("sudo config save -y", module_ignore_errors=True)


# ---------------------------------------------------------------------------
# TC-N1: CLI rejects unknown prefix type
# ---------------------------------------------------------------------------
@pytest.mark.topology("any")
def test_unsupported_prefix_type_cli(duthosts, rand_one_dut_hostname):
    """TC-N1: `prefix_list add UNKNOWN_TYPE ...` exits non-zero, no DB key."""
    duthost = duthosts[rand_one_dut_hostname]
    pytest_require(not duthost.is_supervisor_node(),
                   "Test requires a frontend duthost")

    unknown = "UNKNOWN_TYPE"
    prefix = "10.99.0.0/24"
    res = op_prefix_with_cmd(duthost, unknown, prefix,
                             "add", ignore_error=True)
    pytest_assert(res["rc"] != 0,
                  "CLI must reject unknown prefix type, got rc=0")
    combined = (res.get("stderr") or "") + " " + (res.get("stdout") or "")
    pytest_assert(
        "supported_prefix_types" in combined
        or "not supported" in combined.lower(),
        "Stderr should reference supported_prefix_types: {}".format(combined),
    )

    keys = duthost.shell(
        'sonic-db-cli CONFIG_DB keys "PREFIX_LIST|{}|*"'.format(unknown),
        module_ignore_errors=True)["stdout"].strip()
    pytest_assert(
        not keys,
        "CONFIG_DB should not contain a key for unknown type: {}".format(keys),
    )


# ---------------------------------------------------------------------------
# TC-N2: Unsupported prefix type written directly to CONFIG_DB is warned
# ---------------------------------------------------------------------------
@pytest.mark.topology("any")
def test_unsupported_prefix_type_direct_db(duthosts, rand_one_dut_hostname):
    """TC-N2: Direct CONFIG_DB write of an unsupported type logs a warning
    and does not affect FRR or crash bgpcfgd."""
    duthost = duthosts[rand_one_dut_hostname]
    pytest_require(not duthost.is_supervisor_node(),
                   "Test requires a frontend duthost")

    unknown = "FOO_TYPE"
    prefix = "10.99.0.0/24"
    db_key = "PREFIX_LIST|{}|{}".format(unknown, prefix)
    try:
        duthost.shell(
            'sonic-db-cli CONFIG_DB hset "{}" NULL NULL'.format(db_key))
        pytest_assert(
            wait_until(30, 2, 0,
                       _syslog_contains, duthost,
                       "Prefix type '{}' is not supported".format(unknown)),
            "bgpcfgd should warn that {} is not supported".format(unknown),
        )
        pytest_assert(
            duthost.is_service_running("bgpcfgd", "bgp"),
            "bgpcfgd crashed after unsupported-type direct DB write",
        )
    finally:
        duthost.shell(
            'sonic-db-cli CONFIG_DB del "{}"'.format(db_key),
            module_ignore_errors=True)


# ---------------------------------------------------------------------------
# TC-N3: Malformed prefix is rejected (CLI) and warned (direct DB)
# ---------------------------------------------------------------------------
@pytest.mark.topology("any")
def test_malformed_prefix(duthosts, rand_one_dut_hostname):
    """TC-N3: CLI rejects malformed prefix; direct DB write produces warning."""
    duthost = duthosts[rand_one_dut_hostname]
    pytest_require(not duthost.is_supervisor_node(),
                   "Test requires a frontend duthost")

    bad_cli = "999.999.0.0/24"
    res = op_prefix_with_cmd(duthost, SUPPRESS_TYPE, bad_cli,
                             "add", ignore_error=True)
    pytest_assert(res["rc"] != 0,
                  "CLI must reject malformed prefix, got rc=0")

    bad_db_value = "not-a-prefix"
    db_key = "PREFIX_LIST|{}|{}".format(SUPPRESS_TYPE, bad_db_value)
    try:
        duthost.shell(
            'sonic-db-cli CONFIG_DB hset "{}" NULL NULL'.format(db_key))
        pytest_assert(
            wait_until(30, 2, 0,
                       _syslog_contains, duthost,
                       "format is wrong for prefix list"),
            "bgpcfgd should warn about malformed prefix",
        )
        pytest_assert(
            duthost.is_service_running("bgpcfgd", "bgp"),
            "bgpcfgd crashed after malformed-prefix direct DB write",
        )
    finally:
        duthost.shell(
            'sonic-db-cli CONFIG_DB del "{}"'.format(db_key),
            module_ignore_errors=True)


# ---------------------------------------------------------------------------
# TC-N4: `status` is allowed on every device (read-only)
# ---------------------------------------------------------------------------
@pytest.mark.topology("any")
def test_status_allowed_on_any_device(duthosts, rand_one_dut_hostname):
    """TC-N4: `prefix_list status` exits 0 on every device (no config change)."""
    duthost = duthosts[rand_one_dut_hostname]
    res = duthost.shell("sudo prefix_list status", module_ignore_errors=True)
    pytest_assert(res["rc"] == 0,
                  "`prefix_list status` must exit 0; got rc={} stderr={}"
                  .format(res["rc"], res.get("stderr")))


# ---------------------------------------------------------------------------
# TC-N5: chassis_supervisor skip behaviour is unchanged
# ---------------------------------------------------------------------------
@pytest.mark.topology("t2")
def test_chassis_supervisor_skip(duthosts):
    """TC-N5: On the supervisor card, prefix_list short-circuits cleanly and
    does not write CONFIG_DB."""
    sup_list = [d for d in duthosts if d.is_supervisor_node()]
    if not sup_list:
        pytest.skip("No supervisor node in this testbed")
    duthost = sup_list[0]

    add_res = op_prefix_with_cmd(duthost, SUPPRESS_TYPE, "10.99.99.0/24",
                                 "add", ignore_error=True)
    status_res = duthost.shell("sudo prefix_list status",
                               module_ignore_errors=True)

    # The skip path is expected to exit cleanly. We accept rc=0 (skip) and
    # require no crash. Some implementations may emit rc=0 with a "skip"
    # banner; either is acceptable so long as nothing is written to
    # CONFIG_DB.
    pytest_assert(add_res["rc"] == 0,
                  "add on supervisor must short-circuit (rc=0); got {}"
                  .format(add_res))
    pytest_assert(status_res["rc"] == 0,
                  "status on supervisor must short-circuit (rc=0); got {}"
                  .format(status_res))

    # Give bgpcfgd a moment in case the write somehow leaked through.
    time.sleep(2)
    keys = duthost.shell(
        'sonic-db-cli CONFIG_DB keys "PREFIX_LIST|{}|*"'.format(SUPPRESS_TYPE),
        module_ignore_errors=True)["stdout"].strip()
    pytest_assert(
        not keys,
        "Supervisor should not have written to CONFIG_DB: {}".format(keys),
    )
