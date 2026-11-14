"""Small synthetic copies of the public reference files, in the same layout as the real ones."""

import zipfile

import pytest

FDA_PRODUCT = (
    "PRODUCTID\tPRODUCTNDC\tPRODUCTTYPENAME\tPROPRIETARYNAME\tPROPRIETARYNAMESUFFIX\tNONPROPRIETARYNAME\t"
    "DOSAGEFORMNAME\tROUTENAME\tSTARTMARKETINGDATE\tENDMARKETINGDATE\tMARKETINGCATEGORYNAME\tAPPLICATIONNUMBER\t"
    "LABELERNAME\tSUBSTANCENAME\tACTIVE_NUMERATOR_STRENGTH\tACTIVE_INGRED_UNIT\tPHARM_CLASSES\tDEASCHEDULE\t"
    "NDC_EXCLUDE_FLAG\tLISTING_RECORD_CERTIFIED_THROUGH\n"
    "11111-0001_aaa\t11111-0001\tHUMAN PRESCRIPTION DRUG\tTestumab\t\ttestumab\tINJECTION, SOLUTION\tINTRAVENOUS\t"
    "20200101\t\tBLA\tBLA000001\tSynthetic Pharma\tTESTUMAB\t100\tmg/10mL\t\t\tN\t20271231\n"
    "2222-0333_bbb\t2222-0333\tHUMAN PRESCRIPTION DRUG\tPillex\t\tpillex\tTABLET\tORAL\t"
    "20190101\t\tNDA\tNDA000002\tFake Labs\tPILLEX\t5\tmg/1\t\t\tN\t20271231\n"
)
FDA_PACKAGE = (
    "PRODUCTID\tPRODUCTNDC\tNDCPACKAGECODE\tPACKAGEDESCRIPTION\tSTARTMARKETINGDATE\tENDMARKETINGDATE\t"
    "NDC_EXCLUDE_FLAG\tSAMPLE_PACKAGE\n"
    "11111-0001_aaa\t11111-0001\t11111-0001-01\t1 VIAL, SINGLE-DOSE in 1 CARTON (11111-0001-01)  / 10 mL in 1 VIAL, SINGLE-DOSE\t20200101\t\tN\tN\n"
    "2222-0333_bbb\t2222-0333\t2222-0333-30\t30 TABLET in 1 BOTTLE (2222-0333-30)\t20190101\t\tN\tN\n"
)
FDA_EXCLUDED_PRODUCT = FDA_PRODUCT.splitlines()[0] + "\n" + (
    "33333-0444_ccc\t33333-0444\tHUMAN PRESCRIPTION DRUG\tOldrug\t\toldrug\tCAPSULE\tORAL\t"
    "20100101\t20201231\tANDA\tANDA000003\tGone Inc\tOLDRUG\t10\tmg/1\t\t\tE\t\n"
)
FDA_EXCLUDED_PACKAGE = FDA_PACKAGE.splitlines()[0] + "\n" + (
    "33333-0444_ccc\t33333-0444\t33333-0444-1\t100 CAPSULE in 1 BOTTLE (33333-0444-1)\t20100101\t20201231\tE\tN\n"
)

CROSSWALK_CSV = (
    "October 2026 ASP NDC - HCPCS Crosswalk for Medicare Part B Drugs,,,,,,,,,\n"
    '"     Effective October 1, 2026 through December 31, 2026",,,,,,,,,\n'
    ",,,,,,,,,\n"
    "Intro text about the crosswalk.,,,,,,,,,\n"
    "_2026_CODE,Short Description,LABELER NAME,NDC,Drug Name,HCPCS dosage,PKG SIZE,PKG QTY,BILLUNITS,BILLUNITSPKG\n"
    "J9999,Testumab inj,Synthetic Pharma,11111-0001-01,Testumab ,10 MG,10,1,10,10\n"
    "J8888,Testumab alt,Synthetic Pharma,11111-0001-01,Testumab,100 MG,10,1,1,1\n"
)
ASP_CSV = (
    "Payment Allowance Limits for Medicare Part B Drugs,,,,,,,,,,,,\n"
    ",,,,,,,,,,,,\n"
    '"Effective October 1, 2026 through December 31, 2026",,,,,,,,,,,,\n'
    "HCPCS Code,Short Description,HCPCS Code Dosage,Payment Limit,Co-insurance Percentage,Vaccine AWP%,"
    "Vaccine Limit,Blood AWP%,Blood limit,Clotting Factor,Notes,,\n"
    "J9999,Testumab inj,10 MG,12.345,20.000,,,,,,,,\n"
    "J8888,Testumab alt,100 MG,\"1,234.50\",20.000,,,,,,,,\n"
    ",,,,,,,,,,,,\n"
    "Footnote: something,,,,,,,,,,,,\n"
)
NADAC_CSV = (
    "NDC Description,NDC,NADAC Per Unit,Effective Date,Pricing Unit,Pharmacy Type Indicator,OTC,"
    "Explanation Code,Classification for Rate Setting,Corresponding Generic Drug NADAC Per Unit,"
    "Corresponding Generic Drug Effective Date,As of Date\n"
    "PILLEX 5 MG TABLET,02222033330,0.50,10/01/2026,EA,C/I,N,1,G,,,10/07/2026\n"
    "PILLEX 5 MG TABLET,02222033330,0.55,11/01/2026,EA,C/I,N,1,G,,,11/04/2026\n"
    "OLDRUG 10 MG CAP,33333044401,1.25,11/01/2026,EA,C/I,N,1,G,,,11/04/2026\n"
)


def _zip(path, members: dict[str, str]):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return path


@pytest.fixture
def raw_files(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    return {
        "fda": _zip(d / "ndctext.zip", {"product.txt": FDA_PRODUCT, "package.txt": FDA_PACKAGE}),
        "fda_excluded": _zip(
            d / "ndc_excluded.zip",
            {"Products_excluded.txt": FDA_EXCLUDED_PRODUCT, "Packages_excluded.txt": FDA_EXCLUDED_PACKAGE},
        ),
        "crosswalk": _zip(
            d / "october-2026-ndc-hcpcs-crosswalk-final.zip",
            {
                "NDC-HCPCS Crosswalk Introduction Text.xls": "binary",
                "section 508 version of NDC-HCPCS Crosswalk Introduction Text 090326.csv": "intro",
                "section 508 version of October 2026 AWP NDC-HCPCS Crosswalk.csv": "not this one",
                "section 508 version of October 2026 ASP NDC-HCPCS Crosswalk 091126.csv": CROSSWALK_CSV,
            },
        ),
        "asp": _zip(
            d / "october-2026-medicare-part-b-payment-limit-files-final.zip",
            {
                "section 508 version of October 2026 Drugs Not Payable Under Part B 091426.csv": "nope",
                "section 508 version of October 2026 Medicare Part B Payment Limit File 091626.csv": ASP_CSV,
            },
        ),
        "nadac": _write(d / "nadac.csv", NADAC_CSV),
    }


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path
