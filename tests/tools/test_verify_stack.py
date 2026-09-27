"""tools/verify_stack.py against small synthetic build artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import verify_stack as vs
from ctag_tools_helpers import append, edit, small_elf, stub_edt

MATRIX = vs.load_targets()
QFAB = vs.soc_geometry(MATRIX, "nrf51822_qfab")
NRF52832 = vs.soc_geometry(MATRIX, "nrf52832_qfaa")

CONTROLLER_CHECKS = {
    "kconfig.ll_sw_split",
    "kconfig.no_softdevice",
    "kconfig.no_mpsl",
    "kconfig.flash_sync",
    "dt.chosen_bt_hci",
    "dt.no_sdc_okay",
    "dt.generated_header",
    "map.no_sdc_libs",
    "map.no_sdc_symbols",
    "map.ll_symbols",
}


def statuses(report: vs.Report) -> dict[str, str]:
    return {c.id: c.status for c in report.checks}


def failed(report: vs.Report) -> set[str]:
    return {c.id for c in report.checks if c.status == "fail"}


def test_zephyr_controller_build_passes(artifacts):
    report = vs.verify(artifacts("zephyr_ctlr"), QFAB, "tag")
    assert report.ok, vs.format_report(report)
    assert report.dt_method == "devicetree_generated.h"
    assert statuses(report)["kconfig.mesh_adv"] == "skip"
    assert CONTROLLER_CHECKS <= {c.id for c in report.checks if c.status == "pass"}


def test_sdc_build_fails_every_controller_check(artifacts):
    report = vs.verify(artifacts("sdc"), NRF52832, "bridge")
    assert not report.ok
    assert CONTROLLER_CHECKS <= failed(report)
    assert statuses(report)["kconfig.ctlr_crypto"] == "pass"


@pytest.mark.parametrize(
    ("file", "old", "new", "expected"),
    [
        (".config", "CONFIG_BT_LL_SW_SPLIT=y", "# CONFIG_BT_LL_SW_SPLIT is not set", {"kconfig.ll_sw_split"}),
        (
            ".config",
            "# CONFIG_BT_LL_SOFTDEVICE is not set",
            "CONFIG_BT_LL_SOFTDEVICE_PERIPHERAL=y",
            {"kconfig.no_softdevice"},
        ),
        (".config", "# CONFIG_MPSL_FEM_ONLY is not set", "CONFIG_MPSL=y", {"kconfig.no_mpsl"}),
        (
            ".config",
            "CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER=y",
            "CONFIG_SOC_FLASH_NRF_RADIO_SYNC_MPSL=y",
            {"kconfig.flash_sync"},
        ),
        (".config", "CONFIG_BT_CTLR_CRYPTO=y", "# CONFIG_BT_CTLR_CRYPTO is not set", {"kconfig.ctlr_crypto"}),
        (
            "zephyr.map",
            "LOAD zephyr/drivers/entropy/libdrivers__entropy.a",
            "LOAD /ncs/nrfxlib/mpsl/lib/nrf52/soft-float/libmpsl.a",
            {"map.no_sdc_libs"},
        ),
        (
            "zephyr.map",
            " .text.bt_hci_entropy_get",
            " .text.sdc_hci_cmd_le_set_adv_enable",
            {"map.no_sdc_symbols"},
        ),
        (
            "zephyr.map",
            "0x0000936c                ll_adv_enable",
            "0x0000936c                ll_adv_disable",
            set(),  # the input section name .text.ll_adv_enable still defines it
        ),
        (
            "devicetree_generated.h",
            "#define DT_COMPAT_HAS_OKAY_zephyr_bt_hci_entropy 1",
            "#define DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc 1",
            {"dt.no_sdc_okay", "dt.generated_header"},
        ),
        (
            "devicetree_generated.h",
            "#define DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split 1",
            "",
            {"dt.generated_header"},
        ),
        (
            "devicetree_generated.h",
            'bt_hci_controller_P_compatible {"zephyr,bt-hci-ll-sw-split"}',
            'bt_hci_controller_P_compatible {"nordic,bt-hci-sdc"}',
            {"dt.chosen_bt_hci"},
        ),
    ],
)
def test_single_sdc_signal_fails_its_check(artifacts, file, old, new, expected):
    art = artifacts("zephyr_ctlr")
    edit(art / file, old, new)
    report = vs.verify(art, QFAB, "tag")
    assert failed(report) == expected
    assert report.ok == (not expected)


def test_missing_ll_symbol_fails(artifacts):
    art = artifacts("zephyr_ctlr")
    edit(art / "zephyr.map", " .text.ll_adv_enable\n", "")
    edit(art / "zephyr.map", "0x0000936c                ll_adv_enable", "0x0000936c                other_symbol")
    report = vs.verify(art, QFAB, "tag")
    assert failed(report) == {"map.ll_symbols"}
    assert "ll_adv_enable" in next(c.detail for c in report.checks if c.id == "map.ll_symbols")


def test_discarded_input_sections_do_not_count_as_linked(artifacts):
    art = artifacts("zephyr_ctlr")
    discarded = "Discarded input sections\n\n .text.ll_adv_enable\n                0x00000000       0x1f4 x.a(ull_adv.c.obj)\n\n"
    edit(art / "zephyr.map", "Memory Configuration", discarded + "Memory Configuration")
    edit(
        art / "zephyr.map",
        " .text.ll_adv_enable\n                0x0000936c",
        " .text.other\n                0x0000936c",
    )
    edit(art / "zephyr.map", "0x0000936c                ll_adv_enable", "0x0000936c                other")
    assert failed(vs.verify(art, QFAB, "tag")) == {"map.ll_symbols"}


def test_integer_mpsl_symbols_and_bt_hci_entropy_do_not_trip(artifacts):
    art = artifacts("zephyr_ctlr")
    text = (art / ".config").read_text(encoding="utf-8")
    assert "CONFIG_MPSL_WORK_STACK_SIZE=1024" in text
    assert "CONFIG_SOC_FLASH_NRF_RADIO_SYNC_MPSL_TIMESLOT_SESSION_COUNT=0" in text
    assert "DT_COMPAT_HAS_OKAY_zephyr_bt_hci_entropy 1" in (art / "devicetree_generated.h").read_text(encoding="utf-8")
    assert vs.verify(art, QFAB, "tag").ok


@pytest.mark.parametrize(("role", "status"), [("bridge", "fail"), ("gateway", "fail"), ("tag", "skip")])
def test_legacy_mesh_advertiser_rejected_on_mesh_roles(artifacts, role, status):
    art = artifacts("zephyr_ctlr")
    append(art / ".config", "CONFIG_BT_MESH=y\nCONFIG_BT_MESH_ADV_LEGACY=y\n")
    assert statuses(vs.verify(art, QFAB, role))["kconfig.mesh_adv"] == status


def test_extended_mesh_advertiser_passes_on_bridge(artifacts):
    art = artifacts("zephyr_ctlr")
    append(art / ".config", "CONFIG_BT_MESH=y\nCONFIG_BT_MESH_ADV_EXT=y\n")
    assert statuses(vs.verify(art, QFAB, "bridge"))["kconfig.mesh_adv"] == "pass"


def test_cryptocell_entropy_is_a_warning(artifacts):
    art = artifacts("zephyr_ctlr")
    append(art / ".config", "CONFIG_ENTROPY_CC3XX=y\n")
    report = vs.verify(art, QFAB, "tag")
    assert statuses(report)["kconfig.entropy"] == "warn"
    assert report.ok


def test_geometry_must_match_the_soc_exactly(artifacts):
    art = artifacts("zephyr_ctlr")
    qfaa = vs.soc_geometry(MATRIX, "nrf51822_qfaa")  # 256 KiB: the DT says 128 KiB
    assert failed(vs.verify(art, qfaa, "tag")) == {"geometry.dt"}
    big_ram = vs.SocGeometry("fake", "nrf51", 131072, 32768)
    assert failed(vs.verify(art, big_ram, "tag")) == {"geometry.dt", "geometry.map"}


def test_flash_region_beyond_soc_flash_fails(artifacts):
    art = artifacts("zephyr_ctlr")
    edit(
        art / "zephyr.map",
        "FLASH            0x00000000         0x0001f000",
        "FLASH            0x00000000         0x00040000",
    )
    assert "geometry.map" in failed(vs.verify(art, QFAB, "tag"))


def test_flash_region_over_storage(artifacts):
    art = artifacts("zephyr_ctlr")
    edit(art / "zephyr.map", "0x0001f000         xr", "0x00020000         xr")
    assert failed(vs.verify(art, QFAB, "tag")) == {"geometry.storage"}
    (art / "zephyr.elf").write_bytes(small_elf())  # the image itself ends far below storage
    report = vs.verify(art, QFAB, "tag")
    assert statuses(report)["geometry.storage"] == "warn"
    assert report.ok


def test_memory_usage_from_elf(artifacts):
    art = artifacts("zephyr_ctlr")
    (art / "zephyr.elf").write_bytes(small_elf())
    memory = vs.verify(art, QFAB, "tag").memory
    assert memory == {
        "flash_region": 0x1F000,
        "flash_used": 0x1040,
        "flash_free": 0x1F000 - 0x1040,
        "flash_used_pct": round(100 * 0x1040 / 0x1F000, 2),
        "ram_region": 0x4000,
        "ram_used": 0xA40,
        "ram_free": 0x4000 - 0xA40,
    }


def test_elf_parser_rejects_non_elf():
    region = vs.Region("FLASH", 0, 0x1000)
    with pytest.raises(ValueError):
        vs.elf_memory_usage(b"not an elf file at all", region, region)


def test_edtlib_path(artifacts):
    art = artifacts("zephyr_ctlr")
    report = vs.verify(art, QFAB, "tag", edt=stub_edt())
    assert report.dt_method == "edtlib"
    assert report.ok, vs.format_report(report)


def test_edtlib_path_detects_sdc(artifacts):
    report = vs.verify(artifacts("zephyr_ctlr"), QFAB, "tag", edt=stub_edt("nordic,bt-hci-sdc"))
    assert failed(report) == {"dt.chosen_bt_hci", "dt.no_sdc_okay"}


def test_unloadable_pickle_falls_back_to_header(artifacts):
    art = artifacts("zephyr_ctlr")
    (art / "edt.pickle").write_bytes(b"not a pickle")
    report = vs.verify(art, QFAB, "tag", zephyr_base=str(art / "no-zephyr-here"))
    assert report.dt_method == "devicetree_generated.h"
    assert report.dt_note
    assert report.ok


def test_zephyr_build_directory_layout(tmp_path, artifacts):
    flat = artifacts("zephyr_ctlr")
    build = tmp_path / "build"
    (build / "zephyr" / "include" / "generated" / "zephyr").mkdir(parents=True)
    (build / "zephyr" / ".config").write_bytes((flat / ".config").read_bytes())
    (build / "zephyr" / "zephyr.map").write_bytes((flat / "zephyr.map").read_bytes())
    header = flat / "devicetree_generated.h"
    (build / "zephyr" / "include" / "generated" / "zephyr" / header.name).write_bytes(header.read_bytes())
    assert vs.verify(build, QFAB, "tag").ok


def test_missing_artifacts_fail(tmp_path):
    report = vs.verify(tmp_path, QFAB, "tag")
    assert not report.ok
    assert {"kconfig", "dt", "map"} <= failed(report)


def test_cli_json_and_exit_codes(artifacts, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert vs.main([str(artifacts("zephyr_ctlr")), "--target", "tag-laowu-bw", "--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["target"] == "tag-laowu-bw"
    assert data["dt_method"] == "devicetree_generated.h"
    assert {c["id"] for c in data["checks"]} >= CONTROLLER_CHECKS
    assert "RESULT: PASS" in capsys.readouterr().out

    assert vs.main([str(artifacts("sdc")), "--soc", "nrf52832_qfaa", "--role", "bridge", "--quiet"]) == 1
    assert capsys.readouterr().out.startswith("RESULT: FAIL")

    assert vs.main([str(tmp_path), "--target", "no-such-target"]) == 2
    assert vs.main([str(tmp_path)]) == 2
    assert vs.main([str(tmp_path / "missing"), "--target", "tag-laowu-bw"]) == 2


def test_every_matrix_soc_is_well_formed():
    for name in MATRIX["socs"]:
        soc = vs.soc_geometry(MATRIX, name)
        assert soc.series in {"nrf51", "nrf52"}
        assert soc.flash % 1024 == 0 and soc.ram % 1024 == 0


def test_map_region_parser_ignores_default():
    text = Path(__file__).with_name("fixtures").joinpath("sdc", "zephyr.map").read_text(encoding="utf-8")
    regions = vs.parse_map_regions(text)
    assert set(regions) == {"FLASH", "RAM", "IDT_LIST"}
    assert regions["RAM"] == vs.Region("RAM", 0x20000000, 0x10000)
