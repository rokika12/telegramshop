import hashlib
import io
import asyncio
import secrets
import socket
import struct
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp
import qrcode
from PIL import Image, ImageDraw, ImageFont

DEFAULT_PROFILE_ID = "OpYY4C0yNljQkGRUdLBJbw9xvfjiYwyQ"
DEFAULT_GATEWAY_URL = "https://khqr.cc/api/payment/requestv2"
DEFAULT_VERIFY_URL = (
    "https://khqr.cc/api/OpYY4C0yNljQkGRUdLBJbw9xvfjiYwyQ/"
    "payment-gateway/v1/payments/check-transv2-khqrcc"
)
PAYMENT_CHECK_WINDOW_SECONDS = 180
PUBLIC_DNS_SERVERS = ("1.1.1.1", "8.8.8.8")


def _dns_query_packet(host: str, query_type: int, query_id: int) -> bytes:
    labels = host.rstrip(".").encode("idna").split(b".")
    question = b"".join(bytes((len(label),)) + label for label in labels) + b"\0"
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question + struct.pack(
        "!HH", query_type, 1
    )


def _dns_name_end(packet: bytes, offset: int) -> int:
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length
    raise OSError("Invalid DNS response")


def _parse_dns_addresses(packet: bytes, query_id: int, query_type: int) -> list[tuple[str, int]]:
    if len(packet) < 12:
        raise OSError("Incomplete DNS response")
    response_id, flags, question_count, answer_count, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if response_id != query_id or not flags & 0x8000 or flags & 0x000F:
        raise OSError("Public DNS lookup failed")

    offset = 12
    for _ in range(question_count):
        offset = _dns_name_end(packet, offset) + 4

    address_size = 4 if query_type == 1 else 16
    family = socket.AF_INET if query_type == 1 else socket.AF_INET6
    answers: list[tuple[str, int]] = []
    for _ in range(answer_count):
        offset = _dns_name_end(packet, offset)
        if offset + 10 > len(packet):
            raise OSError("Invalid DNS answer")
        record_type, record_class, ttl, data_length = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        record_end = offset + data_length
        if record_end > len(packet):
            raise OSError("Invalid DNS answer data")
        if record_class == 1 and record_type == query_type and data_length == address_size:
            address = socket.inet_ntop(family, packet[offset:record_end])
            answers.append((address, ttl))
        offset = record_end
    if not answers:
        raise OSError("Public DNS returned no address")
    return answers


async def _public_dns_lookup(host: str, query_type: int) -> list[tuple[str, int]]:
    loop = asyncio.get_running_loop()
    query_id = secrets.randbelow(65536)
    packet = _dns_query_packet(host, query_type, query_id)
    last_error: OSError | None = None
    for server in PUBLIC_DNS_SERVERS:
        dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dns_socket.setblocking(False)
        try:
            await loop.sock_connect(dns_socket, (server, 53))
            await loop.sock_sendall(dns_socket, packet)
            response = await asyncio.wait_for(loop.sock_recv(dns_socket, 4096), timeout=2.5)
            return _parse_dns_addresses(response, query_id, query_type)
        except (OSError, asyncio.TimeoutError) as error:
            last_error = error if isinstance(error, OSError) else OSError("Public DNS lookup timed out")
        finally:
            dns_socket.close()
    raise OSError("Could not resolve KHQRcc through public DNS") from last_error


class KhqrDnsResolver(aiohttp.abc.AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        normalized_host = host.rstrip(".").lower()
        if normalized_host != "khqr.cc" and not normalized_host.endswith(".khqr.cc"):
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
            )
            return [
                {
                    "hostname": host,
                    "host": info[4][0],
                    "port": port,
                    "family": info[0],
                    "proto": info[2],
                    "flags": info[3],
                }
                for info in infos
            ]

        query_type = 28 if family == socket.AF_INET6 else 1
        address_family = socket.AF_INET6 if query_type == 28 else socket.AF_INET
        addresses = await _public_dns_lookup(normalized_host, query_type)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": 0,
                "flags": 0,
            }
            for address, _ in addresses
        ]

    async def close(self) -> None:
        return None


def _client_session(timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=KhqrDnsResolver(), use_dns_cache=False)
    return aiohttp.ClientSession(timeout=timeout, connector=connector)


@dataclass(frozen=True)
class PaymentCheck:
    status: str
    amount_cents: int | None = None


@dataclass(frozen=True)
class DirectQr:
    content: str = ""
    image_url: str = ""


def amount_text(price_cents: int) -> str:
    return format((Decimal(price_cents) / Decimal(100)).normalize(), "f")


def direct_qr_amount_text(price_cents: int) -> str:
    return f"{Decimal(price_cents) / Decimal(100):.2f}"


def direct_qr_endpoint(profile_id: str) -> str:
    return f"https://khqr.cc/api/{profile_id}/payment-gateway/v1/payments/qr-api-khqrcc"


def direct_qr_form(
    *,
    secret_key: str,
    transaction_id: str,
    amount: str,
    success_url: str,
    remark: str,
) -> dict[str, str]:
    signature = hashlib.sha1(
        f"{secret_key}{transaction_id}{amount}{success_url}{remark}".encode("utf-8")
    ).hexdigest()
    return {
        "transaction_id": transaction_id,
        "amount": amount,
        "success_url": success_url,
        "remark": remark,
        "hash": signature,
    }


def _nested_value(payload: Any, keys: tuple[str, ...]) -> str:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("data", "result", "payload", "qr", "qrCode", "qr_code", "emv"):
            if key in payload:
                value = _nested_value(payload[key], keys)
                if value:
                    return value
    elif isinstance(payload, list):
        for item in payload:
            value = _nested_value(item, keys)
            if value:
                return value
    return ""


def parse_direct_qr_response(payload: object) -> DirectQr:
    if not isinstance(payload, (dict, list)):
        raise ValueError("KHQRcc returned an invalid QR response")
    if not _direct_qr_succeeded(payload):
        raise ValueError("KHQRcc did not confirm QR generation")

    qr_content = _nested_value(
        payload,
        (
            "qr",
            "qrContent",
            "qr_content",
            "qrString",
            "qr_string",
            "qrData",
            "qr_data",
            "qrCode",
            "qr_code",
            "emv",
            "emv_code",
            "emvCode",
        ),
    )
    image_url = _nested_value(payload, ("qr_url", "qrUrl", "image_url", "imageUrl"))
    if qr_content and not qr_content.startswith("000201"):
        raise ValueError("KHQRcc returned an invalid KHQR payload")
    if not qr_content and not image_url:
        raise ValueError("KHQRcc response did not include a QR payload or image")
    return DirectQr(content=qr_content, image_url=image_url)


def _direct_qr_succeeded(payload: Any) -> bool:
    if isinstance(payload, dict):
        code = payload.get("responseCode", payload.get("response_code"))
        if str(code).strip() in {"0", "00", "0000"}:
            return True
        status = str(payload.get("status", payload.get("transaction_status", ""))).strip().lower()
        if status in {"success", "successful", "approved"}:
            return True
        return any(
            _direct_qr_succeeded(payload.get(key))
            for key in ("data", "result", "payload", "qr", "qrCode", "qr_code")
        )
    if isinstance(payload, list):
        return any(_direct_qr_succeeded(item) for item in payload)
    return False


async def request_direct_qr(
    *,
    profile_id: str,
    secret_key: str,
    transaction_id: str,
    amount_cents: int,
    success_url: str,
    remark: str,
    session: aiohttp.ClientSession | None = None,
) -> DirectQr:
    owns_session = session is None
    client = session or _client_session(aiohttp.ClientTimeout(total=12, connect=5))
    form = direct_qr_form(
        secret_key=secret_key,
        transaction_id=transaction_id,
        amount=direct_qr_amount_text(amount_cents),
        success_url=success_url,
        remark=remark,
    )
    try:
        async with client.post(direct_qr_endpoint(profile_id), data=form) as response:
            if response.status != 200:
                raise ValueError(f"KHQRcc QR API returned HTTP {response.status}")
            return parse_direct_qr_response(await response.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError) as error:
        raise ValueError(f"KHQRcc Direct QR API connection failed ({type(error).__name__})") from error
    finally:
        if owns_session:
            await client.close()


async def download_direct_qr_image(
    image_url: str,
    session: aiohttp.ClientSession | None = None,
) -> bytes:
    url = urljoin("https://khqr.cc/", image_url)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (hostname == "khqr.cc" or hostname.endswith(".khqr.cc")):
        raise ValueError("KHQRcc returned an untrusted QR image URL")

    owns_session = session is None
    client = session or _client_session(aiohttp.ClientTimeout(total=12, connect=5))
    try:
        async with client.get(url) as response:
            if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                raise ValueError("KHQRcc QR image could not be downloaded")
            if response.content_length and response.content_length > 5_000_000:
                raise ValueError("KHQRcc QR image is too large")
            image = await response.content.read(5_000_001)
            if not image or len(image) > 5_000_000:
                raise ValueError("KHQRcc QR image is invalid")
            return image
    except (aiohttp.ClientError, TimeoutError) as error:
        raise ValueError("KHQRcc QR image could not be downloaded") from error
    finally:
        if owns_session:
            await client.close()


def render_qr_png(content: str) -> bytes:
    if not content.startswith("000201"):
        raise ValueError("Invalid KHQR payload")
    image = qrcode.make(content)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def khqr_merchant_name(content: str) -> str:
    offset = 0
    while offset + 4 <= len(content):
        tag = content[offset : offset + 2]
        length_text = content[offset + 2 : offset + 4]
        if not length_text.isdigit():
            break
        length = int(length_text)
        value_start = offset + 4
        value_end = value_start + length
        if value_end > len(content):
            break
        if tag == "59":
            return content[value_start:value_end].strip() or "ROKIRA"
        offset = value_end
    return "ROKIRA"


def _khqr_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_names = (
        ("segoeuib.ttf", "arialbd.ttf")
        if bold
        else ("segoeui.ttf", "arial.ttf")
    )
    candidates = [Path("C:/Windows/Fonts") / name for name in font_names]
    candidates.extend(
        Path(directory) / name
        for directory in (
            "/usr/share/fonts/truetype/dejavu",
            "/usr/share/fonts/truetype/liberation",
        )
        for name in font_names
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def render_payment_card_png(content: str, amount_cents: int, transaction_id: str) -> bytes:
    if not content.startswith("000201"):
        raise ValueError("Invalid KHQR payload")

    width, height = 900, 1320
    image = Image.new("RGB", (width, height), "#E8ECEF")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, width - 12, height - 12), radius=28, fill="#FFFFFF")
    draw.rounded_rectangle((12, 12, width - 12, 178), radius=28, fill="#E31B23")
    draw.rectangle((12, 105, width - 12, 178), fill="#E31B23")

    def centered_text(text: str, y: int, font: ImageFont.ImageFont, color: str) -> None:
        bounds = draw.textbbox((0, 0), text, font=font)
        x = (width - (bounds[2] - bounds[0])) // 2
        draw.text((x, y), text, font=font, fill=color)

    centered_text("KHQR", 52, _khqr_font(66, bold=True), "#FFFFFF")
    merchant_name = khqr_merchant_name(content)
    draw.text((82, 218), merchant_name, font=_khqr_font(32, bold=True), fill="#17212B")
    draw.text(
        (82, 278),
        f"${Decimal(amount_cents) / Decimal(100):.2f} USD",
        font=_khqr_font(48, bold=True),
        fill="#101820",
    )

    for x in range(28, width - 28, 30):
        draw.line((x, 385, min(x + 14, width - 28), 385), fill="#B7BEC5", width=3)

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=4,
    )
    qr.add_data(content)
    qr.make(fit=True)
    matrix_size = qr.modules_count + qr.border * 2
    qr.box_size = max(1, 600 // matrix_size)
    qr_image = qr.make_image(fill_color="#101820", back_color="#FFFFFF").convert("RGB")
    qr_x = (width - qr_image.width) // 2
    image.paste(qr_image, (qr_x, 410))

    footer_top = 1040
    draw.rounded_rectangle(
        (12, footer_top, width - 12, height - 12),
        radius=28,
        fill="#172736",
    )
    draw.rectangle((12, footer_top, width - 12, footer_top + 28), fill="#172736")
    draw.text((62, 1070), "TRANSACTION ID", font=_khqr_font(24, bold=True), fill="#E8EEF3")
    draw.text((62, 1110), transaction_id, font=_khqr_font(22), fill="#58B9E8")
    draw.text(
        (62, 1170),
        "Scan with ABA Mobile or any KHQR wallet",
        font=_khqr_font(23),
        fill="#E8EEF3",
    )
    draw.text(
        (62, 1215),
        "Payment is verified automatically",
        font=_khqr_font(21),
        fill="#C7D5DF",
    )

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def create_checkout_url(
    *,
    profile_id: str,
    secret_key: str,
    transaction_id: str,
    amount_cents: int,
    success_url: str,
    remark: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
) -> str:
    amount = amount_text(amount_cents)
    signature = hashlib.sha1(
        f"{secret_key}{transaction_id}{amount}{success_url}{remark}".encode("utf-8")
    ).hexdigest()
    query = urlencode(
        {
            "transaction_id": transaction_id,
            "amount": amount,
            "success_url": success_url,
            "remark": remark,
            "hash": signature,
        }
    )
    return f"{gateway_url.rstrip('/')}/{profile_id}?{query}"


def _response_check(payload: object) -> PaymentCheck:
    if not isinstance(payload, dict):
        return PaymentCheck("error")
    response_code = payload.get("responseCode")
    if type(response_code) is not int or response_code != 0:
        return PaymentCheck("error")
    data = payload.get("data")
    if not isinstance(data, dict):
        return PaymentCheck("error")
    status = str(data.get("status", "")).lower()
    if status != "success":
        return PaymentCheck("pending" if status == "pending" else "error")
    raw_amount = data.get("amount")
    if raw_amount is None:
        return PaymentCheck("error")
    try:
        raw_decimal = Decimal(str(raw_amount))
        amount = raw_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not amount.is_finite() or amount <= 0 or raw_decimal != amount:
            return PaymentCheck("error")
    except (InvalidOperation, ValueError):
        return PaymentCheck("error")
    return PaymentCheck("success", int(amount * 100))


async def verify_transaction(
    *,
    transaction_id: str,
    secret_key: str,
    verify_url: str = DEFAULT_VERIFY_URL,
    session: aiohttp.ClientSession | None = None,
) -> PaymentCheck:
    signature = hashlib.sha1(f"{secret_key}{transaction_id}".encode("utf-8")).hexdigest()
    owns_session = session is None
    client = session or _client_session(aiohttp.ClientTimeout(total=10))
    try:
        async with client.post(
            verify_url,
            data={"transaction_id": transaction_id, "hash": signature},
        ) as response:
            if response.status != 200:
                return PaymentCheck("error")
            return _response_check(await response.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return PaymentCheck("error")
    finally:
        if owns_session:
            await client.close()
