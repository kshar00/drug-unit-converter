import pytest

from converter.ndc import NDCFormatError, format_ndc11, normalize_ndc, parse_package_description


# --- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, ndc11, fmt",
    [
        ("0002-1433-61", "00002143361", "4-4-2"),
        ("31722-704-32", "31722070432", "5-3-2"),
        ("70000-0024-1", "70000002401", "5-4-1"),
        ("13533-0636-01", "13533063601", "5-4-2"),
        ("00002143361", "00002143361", "11"),
        (" 0002-1433-61 ", "00002143361", "4-4-2"),
        ("0002–1433–61", "00002143361", "4-4-2"),  # en dashes from copy-paste
    ],
)
def test_normalize_formats(raw, ndc11, fmt):
    n = normalize_ndc(raw)
    assert n.ndc11 == ndc11
    assert n.source_format == fmt
    assert not n.is_ambiguous
    assert n.candidates == (ndc11,)


def test_hyphenated_output():
    assert normalize_ndc("0002-1433-61").hyphenated == "00002-1433-61"
    assert format_ndc11("31722070432") == "31722-0704-32"


def test_unhyphenated_ten_digit_is_ambiguous():
    n = normalize_ndc("0002143361")
    assert n.is_ambiguous
    assert n.ndc11 is None
    assert n.candidates == ("00002143361", "00021043361", "00021433601")
    assert n.warnings


def test_excel_stripped_leading_zeros():
    # 00002143361 typed into Excel becomes 2143361
    for raw in (2143361, 2143361.0, "2143361"):
        n = normalize_ndc(raw)
        assert n.ndc11 == "00002143361"
        assert n.source_format == "short"
        assert n.warnings


@pytest.mark.parametrize(
    "raw",
    ["", None, float("nan"), "abc", "0002-1433", "002-1433-61", "123456789012", "12345", "1.5", 2143361.5, "0002-14x3-61"],
)
def test_invalid(raw):
    with pytest.raises(NDCFormatError):
        normalize_ndc(raw)


# --- package descriptions --------------------------------------------------


def test_tablets_single_level():
    p = parse_package_description("30 TABLET in 1 BOTTLE (0002-4184-30) ")
    assert (p.package_quantity, p.ncpdp_unit, p.is_kit) == (30, "EA", False)
    assert not p.warnings


def test_vials_with_volume():
    p = parse_package_description(
        "10 VIAL, SINGLE-DOSE in 1 CARTON (0002-1433-61)  / 2 mL in 1 VIAL, SINGLE-DOSE"
    )
    assert (p.package_quantity, p.ncpdp_unit) == (20, "ML")
    assert len(p.levels) == 2


def test_decimal_volume_without_leading_zero():
    p = parse_package_description("1 VIAL, SINGLE-DOSE in 1 CARTON (0002-0152-01)  / .5 mL in 1 VIAL, SINGLE-DOSE")
    assert (p.package_quantity, p.ncpdp_unit) == (0.5, "ML")


def test_three_levels():
    p = parse_package_description(
        "10 TRAY in 1 CASE (0904-7321-76)  / 10 CUP, UNIT-DOSE in 1 TRAY / 20.3 mL in 1 CUP, UNIT-DOSE (0904-7321-03) "
    )
    assert (p.package_quantity, p.ncpdp_unit) == (2030, "ML")


def test_blister_packs():
    p = parse_package_description("2 BLISTER PACK in 1 CARTON (23155-858-78)  / 10 CAPSULE in 1 BLISTER PACK")
    assert (p.package_quantity, p.ncpdp_unit) == (20, "EA")


def test_grams_and_litres():
    assert parse_package_description("1 BOTTLE in 1 CARTON (0009-0260-02)  / 500 g in 1 BOTTLE").ncpdp_unit == "GM"
    p = parse_package_description("100 PACKET in 1 BOX (0404-0122-02)  / .0019 L in 1 PACKET (0404-0122-01) ")
    assert (p.package_quantity, p.ncpdp_unit) == (190, "ML")
    p = parse_package_description("1.13 kg in 1 CYLINDER (46123-050-01) ")
    assert (p.package_quantity, p.ncpdp_unit) == (1130, "GM")


def test_old_arrow_separator():
    p = parse_package_description("25 VIAL in 1 CARTON (0409-1234-01) > 2 mL in 1 VIAL")
    assert (p.package_quantity, p.ncpdp_unit) == (50, "ML")


def test_strength_unit_counts_containers():
    # lyophilised powder: "300 mg in 1 VIAL" -> bill each vial as EA
    p = parse_package_description("10 VIAL, SINGLE-USE in 1 CARTON (0121-1101-02)  / 300 mg in 1 VIAL, SINGLE-USE (0121-1101-55) ")
    assert (p.package_quantity, p.ncpdp_unit) == (10, "EA")
    assert p.notes and not p.warnings


def test_patch_duration_counts_patches():
    p = parse_package_description("30 PATCH in 1 CARTON (0078-0501-15)  / 24 h in 1 PATCH (0078-0501-61) ")
    assert (p.package_quantity, p.ncpdp_unit) == (30, "EA")


def test_powder_for_solution_vials():
    p = parse_package_description(
        "10 VIAL, SINGLE-DOSE in 1 CARTON (0003-2560-16)  / 1 INJECTION, POWDER, FOR SOLUTION in 1 VIAL, SINGLE-DOSE"
    )
    assert (p.package_quantity, p.ncpdp_unit) == (10, "EA")
    assert not p.warnings


def test_lyophilised_powder_billed_per_vial():
    # Remicade: FDA lists the 10 mL reconstituted volume, NCPDP bills 1 EA per vial
    desc = "1 VIAL, SINGLE-USE in 1 BOX (57894-030-01)  / 10 mL in 1 VIAL, SINGLE-USE"
    p = parse_package_description(desc, "INJECTION, POWDER, LYOPHILIZED, FOR SOLUTION")
    assert (p.package_quantity, p.ncpdp_unit) == (1, "EA")
    assert p.notes
    # without the dosage form we can only read it as volume
    assert parse_package_description(desc).ncpdp_unit == "ML"


def test_oral_powder_for_suspension_stays_ml():
    # amoxicillin-type bottle: billed in mL of reconstituted suspension
    p = parse_package_description("100 mL in 1 BOTTLE (0093-4155-73) ", "POWDER, FOR SUSPENSION")
    assert (p.package_quantity, p.ncpdp_unit) == (100, "ML")


def test_kit_is_flagged():
    p = parse_package_description(
        "1 KIT in 1 CARTON (0009-7168-01)  *  1 mL in 1 VIAL, SINGLE-DOSE (0009-5177-02)  *  1 mL in 1 SYRINGE (0009-5936-01) "
    )
    assert p.is_kit
    assert (p.package_quantity, p.ncpdp_unit) == (1, "EA")
    assert p.warnings


def test_kit_nested_in_tray():
    p = parse_package_description(
        "1 TRAY in 1 CARTON (0002-8147-01)  / 1 KIT in 1 TRAY *  2.88 mL in 1 CARTRIDGE (0002-7554-01)  *  2.88 mL in 1 SYRINGE"
    )
    assert p.is_kit and p.package_quantity == 1


def test_metered_inhaler_warns():
    p = parse_package_description("1 CANISTER in 1 CARTON (0054-0742-87)  / 200 AEROSOL, METERED in 1 CANISTER")
    assert p.ncpdp_unit == "EA"
    assert any("metered" in w for w in p.warnings)


def test_unstated_volume_warns():
    p = parse_package_description("1 CREAM in 1 BOTTLE (51367-036-75) ")
    assert p.warnings


@pytest.mark.parametrize("desc", [None, "", "   ", "see package insert", "1 VIAL in 0 CARTON"])
def test_unparseable(desc):
    p = parse_package_description(desc)
    assert not p.parsed
    assert p.warnings
