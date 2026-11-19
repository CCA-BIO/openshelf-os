from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import errno
import logging
import os
import threading
from types import TracebackType
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.common import PrinterSettings, RootConfig
from openshelf_os.config.settings import BARCODE_MAX_LENGTH

from openshelf_os_server.core.label_printer import Symbology, build_label_job
from openshelf_os_server.core.printer_status import (
    PrinterStatus,
    PRINTER_UNREADABLE_MESSAGE,
    PrinterStatusProbeError,
    probe_printer_status,
)
from openshelf_os_server.routers.auth_deps import (
    get_current_user,
    require_permissions,
    require_relay_or_permissions,
)
from openshelf_os_server.settings import (
    get_operating_mode,
    get_root_config,
    get_settings,
    update_root_config,
)

logger = logging.getLogger(__name__)


class PrinterConfig(BaseModel):
    feed_dots: int = Field(ge=0, le=128)
    item_symbology: Symbology
    qr_module_dots: int = Field(ge=2, le=8)


class PrinterConfigUpdate(BaseModel):
    """Omitted fields keep their stored value."""

    feed_dots: int | None = Field(None, ge=0, le=128)
    item_symbology: Symbology | None = None
    qr_module_dots: int | None = Field(None, ge=2, le=8)


class PrinterStatusResponse(BaseModel):
    enabled: bool
    connected: bool
    healthy: bool
    simulated: bool = False
    error: str | None = None
    paper_out: bool = False
    paper_near_end: bool = False
    cover_open: bool = False
    cutter_error: bool = False
    recoverable_error: bool = False
    unrecoverable_error: bool = False
    auto_recoverable_error: bool = False


class PrintLabelRequest(BaseModel):
    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH, pattern=r"^[\x20-\x7E]+$")
    # Overrides printer.item_symbology for this label; bin labels are always Code 128.
    symbology: Symbology | None = None


class PrintLabelResponse(BaseModel):
    message: str
    barcode: str
    printed: bool
    symbology: Symbology


class USBPrinter:
    """ESC/POS direct-write printer for /dev/usb/lp0."""

    def __init__(self, device: str = "/dev/usb/lp0"):
        self.device = device
        self._fd: Optional[int] = None

    def open(self) -> None:
        if self._fd is not None:
            return
        self._fd = os.open(self.device, os.O_WRONLY | os.O_CLOEXEC)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self) -> "USBPrinter":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _write_all(self, data: bytes) -> None:
        if self._fd is None:
            self.open()
        try:
            self._write_all_once(data)
        except OSError as e:
            if e.errno in (errno.EPIPE, errno.ENODEV, errno.EIO):
                self.close()
                self.open()
                self._write_all_once(data)
            else:
                raise

    def _write_all_once(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        total = 0
        while total < len(view):
            n = os.write(self._fd, view[total:])
            if n <= 0:
                raise OSError(errno.EIO, "short write to printer")
            total += n

    def print_label(
        self,
        payload: str,
        *,
        feed_dots: int,
        location: str | None = None,
        symbology: Symbology = "code128",
        qr_module_dots: int,
    ) -> None:
        """Write one label job with the configured feed distance to the printer."""
        self._write_all(
            build_label_job(
                payload,
                feed_dots=feed_dots,
                location=location,
                symbology=symbology,
                qr_module_dots=qr_module_dots,
            )
        )


router = APIRouter(tags=["printer"])
printer = USBPrinter()
printer_io_lock = threading.Lock()


def _probe_status() -> PrinterStatus:
    with printer_io_lock:
        return probe_printer_status(printer.device)


def _printer_config(settings: PrinterSettings) -> PrinterConfig:
    return PrinterConfig.model_validate(settings.model_dump(include=set(PrinterConfig.model_fields)))


@router.get(
    "/config",
    response_model=PrinterConfig,
    dependencies=[Depends(get_current_user)],
    summary="Read printer label settings",
    responses=response_docs(errors=(401,)),
)
async def get_printer_config() -> PrinterConfig:
    """Read printer label settings.

    Returns the feed distance in printer dots, the symbology printed on item labels
    (`code128` or `qr`), and the QR module size in dots. Does not print a label.
    """
    return _printer_config(get_root_config().printer)


@router.put(
    "/config",
    response_model=PrinterConfig,
    dependencies=[Depends(get_current_user), Depends(require_permissions("printer:print"))],
    summary="Set printer label settings",
    responses=response_docs(errors=(401, 403)),
)
async def update_printer_config(request: PrinterConfigUpdate) -> PrinterConfig:
    """Set printer label settings.

    Requires `printer:print`. Persists the feed distance used after printing, the item
    label symbology and the QR module size; omitted fields keep their stored value. Bin
    and login labels stay Code 128, and scanning accepts 1D and 2D codes regardless.
    This updates configuration without printing a test label.
    """
    changes = request.model_dump(exclude_none=True)

    def apply(draft: RootConfig) -> None:
        for key, value in changes.items():
            setattr(draft.printer, key, value)

    return _printer_config(update_root_config(apply).printer)


@router.get(
    "/status",
    response_model=PrinterStatusResponse,
    dependencies=[Depends(get_current_user)],
    summary="Read printer diagnostics",
    responses=response_docs(errors=(401,)),
)
async def get_printer_status() -> PrinterStatusResponse:
    """Read printer diagnostics.

    Reports connectivity, health, and printer error flags. A disconnected or unhealthy
    printer can still return HTTP `200`; inspect the body. Mock mode reports
    `simulated=true` and does not contact hardware.
    """
    settings = get_settings()
    if get_operating_mode() == OperatingMode.mock:
        return PrinterStatusResponse(
            enabled=settings.printer.enabled,
            connected=False,
            healthy=True,
            simulated=True,
        )

    try:
        current_status = await asyncio.to_thread(_probe_status)
    except PrinterStatusProbeError as exc:
        logger.warning("printer status probe failed", exc_info=exc)
        return PrinterStatusResponse(
            enabled=settings.printer.enabled,
            connected=False,
            healthy=False,
            error=PRINTER_UNREADABLE_MESSAGE,
        )

    failure_reason = current_status.failure_reason()
    return PrinterStatusResponse(
        enabled=settings.printer.enabled,
        connected=True,
        healthy=failure_reason is None,
        error=failure_reason,
        paper_out=current_status.paper_out,
        paper_near_end=current_status.paper_near_end,
        cover_open=current_status.cover_open,
        cutter_error=current_status.cutter_error,
        recoverable_error=current_status.recoverable_error,
        unrecoverable_error=current_status.unrecoverable_error,
        auto_recoverable_error=current_status.auto_recoverable_error,
    )


def _bin_home_label(payload: str) -> str | None:
    """Home cell of the multi-item bin with this barcode, as ``F:shelf:cell``."""
    for bin_setting in get_root_config().storage.bins:
        if bin_setting.barcode == payload:
            home = bin_setting.home_location
            side = "F" if home.side == "front_module" else "B"
            return f"{side}:{home.shelf_idx}:{home.cell_idx}"
    return None


def _print_payload(payload: str, symbology: Symbology = "code128") -> bool:
    if get_operating_mode() == OperatingMode.mock:
        return True

    location = _bin_home_label(payload)
    settings = get_settings()
    if settings.printer.enabled is False:
        logger.info("Printer disabled; skipping label print for %s", payload)
        return False

    with printer_io_lock:
        try:
            printer.print_label(
                payload,
                feed_dots=settings.printer.feed_dots,
                location=location,
                symbology=symbology,
                qr_module_dots=settings.printer.qr_module_dots,
            )
        finally:
            printer.close()
    return True


def _item_label_symbology(payload: str, requested: Symbology | None) -> Symbology:
    if _bin_home_label(payload) is not None:
        return "code128"
    return requested or get_root_config().printer.item_symbology


@router.post(
    "",
    response_model=PrintLabelResponse,
    dependencies=[Depends(require_relay_or_permissions("printer:print"))],
    summary="Print a barcode label",
    responses=response_docs(errors=(401, 403, 422, 503)),
)
async def print_label(request: PrintLabelRequest) -> PrintLabelResponse:
    """Print a barcode label.

    Requires `printer:print` or the relay service credential. Takes the barcode in the
    request body, so every customer barcode is printable. Item labels use the configured
    `item_symbology` unless the request sets `symbology`: `code128` prints the bars with
    the barcode as text above them, `qr` prints a QR code at the left edge with the
    barcode wrapped beside it, so the label can be trimmed for small items. A multi-item
    bin's label is always Code 128 and adds its home cell (e.g. `F:0:0`). The response
    reports the symbology used. Mock mode simulates printing without a physical artifact.
    This operation has no idempotency key; a retry may print another label. A printer
    write failure returns `503`.
    """
    symbology = _item_label_symbology(request.barcode, request.symbology)
    return PrintLabelResponse(
        message="success",
        barcode=request.barcode,
        printed=await asyncio.to_thread(_print_payload, request.barcode, symbology),
        symbology=symbology,
    )
