"""Build-Skript für den deutschen Offline-Katalog: Filter, Bereinigung, Deduplizierung, Manifest."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_offline_catalog", ROOT / "scripts" / "build_offline_catalog.py")
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)

HEADER = "code\turl\tproduct_name\tproduct_name_de\tbrands\tbrands_tags\tbrands_en\tcountries\tcountries_tags\tcountries_en"


def row(code, name="", name_de="", brands="", tags="", ctags="en:germany", countries="", brands_tags="", brands_en=""):
    return f"{code}\thttp://x\t{name}\t{name_de}\t{brands}\t{brands_tags}\t{brands_en}\t{countries}\t{ctags}\t"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("4337256722261", "4337256722261"),  # ja! Butter
        ("4001686009727", "4001686009727"),  # Haribo Stars
        ("40084060", "40084060"),            # EAN-8
        (" 4000417025005 ", "4000417025005"),
        ("4337256722262", None),             # falsche Prüfziffer
        ("036000291452", None),              # UPC-A (12) gehört nicht in den Katalog
        ("10036000291459", None),            # GTIN-14
        ("0000000000000", None),             # Dummy
        ("00000000", None),
        ("1111111111116", None),             # nur gleiche Ziffern (falls Prüfziffer passt)
        ("0000000123457", None),             # fast nur Nullen
        ("2001234567893", None),             # Präfix 2 = händlerintern (Waage)
        ("４３３７２５６７２２２６１", None),   # Unicode-Ziffern
        ("ABC", None),
        ("", None),
    ],
)
def test_normalize_ean(code, expected):
    assert bc.normalize_ean(code) == expected


def test_checksum_helper():
    assert bc.ean_checksum_ok("4337256722261")
    assert bc.ean_checksum_ok("40084060")
    assert not bc.ean_checksum_ok("40084061")


def test_is_german_strict_tokens():
    assert bc.is_german("en:france,en:germany")
    assert bc.is_german(" EN:GERMANY ")
    assert not bc.is_german("en:germany-north")  # exaktes Token
    assert not bc.is_german("en:austria,en:switzerland")
    # Fallback nur, wenn countries_tags leer ist
    assert bc.is_german("", "Deutschland")
    assert bc.is_german("", "France, Germany")
    assert not bc.is_german("en:france", "Deutschland")
    assert not bc.is_german("", "Germanyland")


def test_clean_name_and_brand():
    assert bc.clean_name("  Butter\\tStreichzart \n  gesalzen ") == "Butter Streichzart gesalzen"
    assert bc.clean_name("x") == ""
    assert bc.clean_name("12345") == ""
    assert bc.clean_name("https://example.org/produkt") == ""
    assert bc.clean_name("www.produkt.de") == ""
    assert len(bc.clean_name("A" * 500)) == bc.MAX_NAME
    assert bc.clean_brand("ja!, Rewe") == "ja!"
    assert bc.clean_brand("", "", "xx:wild-pasture") == "wild-pasture"
    assert bc.clean_brand("", "Hela") == "Hela"
    assert bc.clean_brand("  ") == ""
    assert len(bc.clean_brand("B" * 200)) == bc.MAX_BRAND


def test_iter_filtered_and_dedupe():
    lines = [
        HEADER + "\n",
        row("4337256722261", name="Butter", brands="ja!") + "\n",
        row("4337256722261", name="Butter", name_de="Deutsche Markenbutter", brands="ja!") + "\n",  # besser
        row("4001686009727", name="Stars", brands="Haribo", ctags="en:france") + "\n",  # nicht DE
        row("4001686009727", name="Stars", brands="Haribo", ctags="", countries="Deutschland") + "\n",
        row("4337256722262", name="Falsche Prüfziffer") + "\n",
        row("40084060", name="", brands="Leer") + "\n",
        row("4000417025005", name="Ritter Sport Nuss", brands="", brands_en="Ritter Sport") + "\n",
        "kaputte zeile ohne tabs\n",
    ]
    stats: dict[str, int] = {}
    entries = list(bc.iter_filtered(lines, stats))
    products = bc.dedupe(entries)
    assert products == {
        "4337256722261": ("ja!", "Deutsche Markenbutter"),
        "4001686009727": ("Haribo", "Stars"),
        "4000417025005": ("Ritter Sport", "Ritter Sport Nuss"),
    }
    assert stats["rows"] == 8 and stats["kept"] == 4


def test_iter_filtered_requires_code_column():
    with pytest.raises(ValueError):
        list(bc.iter_filtered(["foo\tbar\n", "1\t2\n"]))


def test_build_writes_tsv_and_manifest(tmp_path):
    src = tmp_path / "dump.csv.gz"
    with gzip.open(src, "wt", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        fh.write(row("4337256722261", name="Butter", brands="ja!") + "\n")
        fh.write(row("4001686009727", name="Stars", brands="Haribo") + "\n")
        fh.write(row("3017620422003", name="Nutella", brands="Ferrero", ctags="en:france") + "\n")
    out = tmp_path / "out"
    manifest = bc.build(out, ["food", "petfood"], {"food": str(src), "petfood": str(src)})
    assert manifest["schema_version"] == 1
    assert manifest["license"] == "Open Database License (ODbL) 1.0"
    assert manifest["contents_license"] == "Database Contents License (DbCL) 1.0"
    assert manifest["contents_license_url"] == "https://opendatacommons.org/licenses/dbcl/1-0/"
    assert "Open Pet Food Facts" in manifest["attribution"] and "DbCL 1.0" in manifest["attribution"]
    assert manifest["generated_at"].endswith("Z")
    food = manifest["categories"]["food"]
    assert food["file"] == "catalog-de-food.tsv.gz" and food["count"] == 2
    assert food["label_de"] == "Lebensmittel & Getränke"
    path = out / food["file"]
    assert food["size_bytes"] == path.stat().st_size
    assert food["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    lines = gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()
    assert lines == ["ean\tbrand\tname", "4001686009727\tHaribo\tStars", "4337256722261\tja!\tButter"]
    on_disk = json.loads((out / "catalog-de-manifest.json").read_text(encoding="utf-8"))
    assert on_disk["categories"]["petfood"]["sha256"] == manifest["categories"]["petfood"]["sha256"]
    assert on_disk["total_count"] == 4
    # deterministisch: gleiche Daten -> gleicher SHA256
    again = bc.build(tmp_path / "out2", ["food"], {"food": str(src)})
    assert again["categories"]["food"]["sha256"] == food["sha256"]


def test_cli_rejects_bad_source(tmp_path):
    with pytest.raises(SystemExit):
        bc.main(["--out", str(tmp_path), "--source", "unbekannt=/x"])
