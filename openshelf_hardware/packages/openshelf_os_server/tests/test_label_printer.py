from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from openshelf_os_server.core import label_printer
from openshelf_os_server.routers import printer as printer_router


def _bin(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        barcode=barcode,
        home_location=SimpleNamespace(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx),
    )


def test_bin_label_names_the_bins_own_home_cell() -> None:
    # A wrong cell on a bin label sends the operator to the wrong storage position.
    config = SimpleNamespace(
        storage=SimpleNamespace(
            bins=[_bin("TRAY-A", "front_module", 0, 0), _bin("TRAY-B", "back_module", 3, 12)]
        )
    )
    with patch.object(printer_router, "get_root_config", return_value=config):
        assert printer_router._bin_home_label("TRAY-A") == "F:0:0"
        assert printer_router._bin_home_label("TRAY-B") == "B:3:12"
        assert printer_router._bin_home_label("ITEM-1") is None


def _decode_qr(image) -> str:
    # Upscaled like a camera close-up; the raw 1-dot-per-pixel raster is too small for OpenCV.
    pixels = cv2.resize(np.array(image), None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    value, *_ = cv2.QRCodeDetector().detectAndDecode(pixels)
    return value


@pytest.mark.parametrize("payload", ["299000001098", "NYU-CHEM-0001-ABCD-7781", "X" * 255])
@pytest.mark.parametrize("module_dots", [2, 4, 8])
def test_qr_label_decodes_to_the_payload(payload: str, module_dots: int) -> None:
    image = label_printer.render_qr_label(payload, module_dots=module_dots)
    assert image.size == (label_printer.LABEL_WIDTH_DOTS, label_printer.LABEL_HEIGHT_DOTS)
    assert _decode_qr(image) == payload


def test_qr_code_sits_flush_left_at_the_requested_size() -> None:
    # Flush left is what lets an operator trim the label down to the code.
    image = label_printer.render_qr_label("299000001098", module_dots=3)
    dark_columns = np.where((np.array(image) < 128).any(axis=0))[0]
    border = label_printer.QR_BORDER_MODULES * 3
    assert dark_columns[0] == label_printer.LEFT_MARGIN_DOTS + border
    # Version 1 is 21 modules wide.
    code_end = dark_columns[0] + 21 * 3
    assert (np.array(image)[:, code_end : code_end + border + label_printer.QR_TEXT_GAP_DOTS] >= 128).all()


def test_qr_module_shrinks_when_the_code_would_not_fit_the_label() -> None:
    image = label_printer.render_qr_label("X" * 255, module_dots=8)
    rows = np.where((np.array(image)[:, :200] < 128).any(axis=1))[0]
    assert rows[0] >= label_printer.TOP_MARGIN_DOTS
    assert rows[-1] < label_printer.LABEL_HEIGHT_DOTS - label_printer.BOTTOM_MARGIN_DOTS


def test_label_job_renders_the_requested_symbology() -> None:
    code128 = label_printer.build_label_job("299000001098", feed_dots=0)
    qr = label_printer.build_label_job("299000001098", feed_dots=0, symbology="qr")
    assert code128 != qr
    assert len(code128) == len(qr)


class _FakePrinter:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def print_label(self, payload: str, **job) -> None:
        self.jobs.append({"payload": payload, **job})

    def close(self) -> None:
        pass


@pytest.fixture
def fake_printer():
    fake = _FakePrinter()
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True, feed_dots=18, qr_module_dots=3))
    with (
        patch.object(printer_router, "printer", fake),
        patch.object(printer_router, "get_settings", return_value=settings),
    ):
        yield fake


@pytest.fixture
async def restore_printer_config(client):
    original = (await client.get("/api/print/config")).json()
    yield
    await client.put("/api/print/config", json=original)


async def test_printer_config_updates_only_the_fields_sent(client, restore_printer_config) -> None:
    original = (await client.get("/api/print/config")).json()
    assert original["item_symbology"] == "code128"

    response = await client.put("/api/print/config", json={"item_symbology": "qr", "qr_module_dots": 3})
    assert response.json() == {"feed_dots": original["feed_dots"], "item_symbology": "qr", "qr_module_dots": 3}

    response = await client.put("/api/print/config", json={"feed_dots": 40})
    assert response.json() == {"feed_dots": 40, "item_symbology": "qr", "qr_module_dots": 3}
    assert (await client.get("/api/print/config")).json() == response.json()

    assert (await client.put("/api/print/config", json={"item_symbology": "datamatrix"})).status_code == 422
    assert (await client.put("/api/print/config", json={"qr_module_dots": 1})).status_code == 422


async def test_item_label_follows_config_and_request_override(client, restore_printer_config, fake_printer) -> None:
    await client.put("/api/print/config", json={"item_symbology": "qr"})

    response = await client.post("/api/print", json={"barcode": "ITEM-1"})
    assert response.json()["symbology"] == "qr"
    response = await client.post("/api/print", json={"barcode": "ITEM-1", "symbology": "code128"})
    assert response.json()["symbology"] == "code128"

    assert [job["symbology"] for job in fake_printer.jobs] == ["qr", "code128"]
    assert fake_printer.jobs[0]["qr_module_dots"] == 3


async def test_bin_label_stays_code128_when_items_print_qr(client, restore_printer_config, fake_printer) -> None:
    await client.put("/api/print/config", json={"item_symbology": "qr"})
    with patch.object(printer_router, "_bin_home_label", return_value="F:0:0"):
        response = await client.post("/api/print", json={"barcode": "TRAY-A", "symbology": "qr"})
    assert response.json()["symbology"] == "code128"
    assert fake_printer.jobs[0]["symbology"] == "code128"
    assert fake_printer.jobs[0]["location"] == "F:0:0"


def test_login_label_print_defaults_to_code128(fake_printer) -> None:
    # users.py prints login badges through _print_payload without a symbology.
    assert printer_router._print_payload("LOGIN-123") is True
    assert fake_printer.jobs[0]["symbology"] == "code128"
