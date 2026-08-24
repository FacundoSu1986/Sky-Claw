"""
POC-002: Formato TES4 y Golden Fixture para Skyrim Special Edition / Anniversary Edition.
"""
import struct
from dataclasses import dataclass
from typing import List, Tuple

from exceptions import MalformedRecordError

SUPPORTED_HEDR_VERSIONS = (0.94, 1.70)
DEFAULT_SSE_HEDR_VERSION = 1.70
DEFAULT_SSE_FORM_VERSION = 44

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_RECORD_SIZE = 1 * 1024 * 1024
MAX_SUBRECORD_SIZE = 64 * 1024
MAX_RECORD_COUNT = 500


def _is_valid_sig(sig: str) -> bool:
    return len(sig) == 4 and all((c.isalnum() or c == "_") for c in sig)


@dataclass(frozen=True)
class Subrecord:
    sig: str
    data: bytes

    def __post_init__(self):
        if not _is_valid_sig(self.sig):
            raise MalformedRecordError(f"Firma de subrecord inválida: {self.sig!r}")
        if len(self.data) > MAX_SUBRECORD_SIZE:
            raise MalformedRecordError(f"Subrecord {self.sig} excede MAX_SUBRECORD_SIZE.")


@dataclass(frozen=True)
class TesRecord:
    sig: str
    flags: int
    form_id: int
    version_control: int
    form_version: int
    subrecords: Tuple[Subrecord, ...]

    def serialize(self) -> bytes:
        body = bytearray()
        for sub in self.subrecords:
            sub_sig = sub.sig.encode("ascii")
            sub_len = len(sub.data)
            if sub_len > 0xFFFF:
                raise MalformedRecordError(f"Subrecord {sub.sig} data_size excede 16 bits.")
            body.extend(sub_sig)
            body.extend(struct.pack("<H", sub_len))
            body.extend(sub.data)

        if len(body) > MAX_RECORD_SIZE:
            raise MalformedRecordError(f"Record {self.sig} serializado excede MAX_RECORD_SIZE.")

        header = bytearray()
        header.extend(self.sig.encode("ascii"))
        header.extend(struct.pack("<I", len(body)))
        header.extend(struct.pack("<I", self.flags))
        header.extend(struct.pack("<I", self.form_id))
        header.extend(struct.pack("<I", self.version_control))
        header.extend(struct.pack("<H", self.form_version))
        header.extend(struct.pack("<H", 0))

        return bytes(header + body)


class StrictPluginParser:
    """Parser fail-closed estricto para records binarios."""

    @staticmethod
    def parse_records(data: bytes) -> Tuple[TesRecord, ...]:
        total_len = len(data)
        if total_len > MAX_FILE_SIZE:
            raise MalformedRecordError(f"Archivo excede MAX_FILE_SIZE ({total_len} > {MAX_FILE_SIZE}).")

        records: List[TesRecord] = []
        offset = 0

        while offset < total_len:
            if offset + 24 > total_len:
                raise MalformedRecordError(f"Record header truncado en offset {offset}.")

            try:
                sig_bytes = data[offset:offset+4]
                sig = sig_bytes.decode("ascii")
                if not _is_valid_sig(sig):
                    raise ValueError()
            except Exception as e:
                raise MalformedRecordError(f"Firma de record inválida en offset {offset}: {data[offset:offset+4]!r}") from e

            data_size, flags, form_id, vc, fv, _ = struct.unpack("<IIIIHH", data[offset+4:offset+24])
            offset += 24

            if data_size > MAX_RECORD_SIZE:
                raise MalformedRecordError(f"Record {sig} data_size {data_size} excede MAX_RECORD_SIZE.")

            if offset + data_size > total_len:
                raise MalformedRecordError("Record payload excede los bytes disponibles en el buffer.")

            sub_data = data[offset:offset+data_size]
            offset += data_size

            subrecords: List[Subrecord] = []
            sub_offset = 0
            while sub_offset < len(sub_data):
                if sub_offset + 6 > len(sub_data):
                    raise MalformedRecordError(f"Subrecord header truncado en sub_offset {sub_offset}.")

                try:
                    s_sig = sub_data[sub_offset:sub_offset+4].decode("ascii")
                    if not _is_valid_sig(s_sig):
                        raise ValueError()
                except Exception as e:
                    raise MalformedRecordError(f"Firma de subrecord inválida: {sub_data[sub_offset:sub_offset+4]!r}") from e

                s_size, = struct.unpack("<H", sub_data[sub_offset+4:sub_offset+6])
                sub_offset += 6

                if s_size > MAX_SUBRECORD_SIZE:
                    raise MalformedRecordError(f"Subrecord {s_sig} data_size {s_size} excede MAX_SUBRECORD_SIZE.")

                if sub_offset + s_size > len(sub_data):
                    raise MalformedRecordError(f"Payload de subrecord {s_sig} truncado.")

                s_payload = sub_data[sub_offset:sub_offset+s_size]
                sub_offset += s_size
                subrecords.append(Subrecord(sig=s_sig, data=s_payload))

            records.append(TesRecord(
                sig=sig,
                flags=flags,
                form_id=form_id,
                version_control=vc,
                form_version=fv,
                subrecords=tuple(subrecords)
            ))

            if len(records) > MAX_RECORD_COUNT:
                raise MalformedRecordError("Número de records excede MAX_RECORD_COUNT.")

        if offset != total_len:
            raise MalformedRecordError(f"Bytes sobrantes no parseados: {total_len - offset} bytes.")

        return tuple(records)


def build_synthetic_tes4_record(
    author: str = "Agent",
    description: str = "Test",
    hedr_version: float = DEFAULT_SSE_HEDR_VERSION,
    form_version: int = DEFAULT_SSE_FORM_VERSION
) -> TesRecord:
    hedr_payload = struct.pack("<fII", hedr_version, 0, 0x00000800)
    cnam_payload = author.encode("utf-8") + b"\x00"
    snam_payload = description.encode("utf-8") + b"\x00"

    subrecords = (
        Subrecord(sig="HEDR", data=hedr_payload),
        Subrecord(sig="CNAM", data=cnam_payload),
        Subrecord(sig="SNAM", data=snam_payload)
    )

    return TesRecord(
        sig="TES4",
        flags=0,
        form_id=0,
        version_control=0,
        form_version=form_version,
        subrecords=subrecords
    )


GOLDEN_AUTHOR = "Agent"
GOLDEN_DESC = "Test"

GOLDEN_SYNTHETIC_TES4_BYTES = bytes([
    0x54, 0x45, 0x53, 0x34,
    0x29, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x2C, 0x00,
    0x00, 0x00,
    0x48, 0x45, 0x44, 0x52,
    0x0C, 0x00,
    0x9A, 0x99, 0xD9, 0x3F,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x08, 0x00, 0x00,
    0x43, 0x4E, 0x41, 0x4D,
    0x06, 0x00,
    0x41, 0x67, 0x65, 0x6E, 0x74, 0x00,
    0x53, 0x4E, 0x41, 0x4D,
    0x05, 0x00,
    0x54, 0x65, 0x73, 0x74, 0x00
])
