"""Download a Cafeyn issue from a reader URL and export it as a PDF."""

import argparse
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import curl_cffi
from dotenv import load_dotenv
from PIL import Image

# AWS ELB in front of api.cafeyn.co does TLS-fingerprint filtering, so we
# impersonate a real Chrome to avoid 404s from the ELB layer that never
# reach the Kestrel backend.
TLS_IMPERSONATE = "chrome131"

try:
    from tqdm import tqdm
except ImportError:

    def tqdm(iterable, **_kwargs):
        """Fallback tqdm implementation when tqdm is unavailable."""
        return iterable


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def sanitize_filename(value):
    """Convert a string to a safe filename."""
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_reader_url(reader_url):
    """Validate and parse a Cafeyn reader URL."""
    match = re.search(r"/ticket/reader/(\d+)/(\d+)", reader_url)
    if not match:
        raise ValueError(
            "Invalid reader URL. Expected format: "
            "https://api.cafeyn.co/ticket/reader/<id>/<id>"
        )
    return match.group(1), match.group(2)


def get_env(name):
    """Read a required environment variable."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required env variable: {name}")
    return value


def fetch_ticket(reader_url, jwt_token, websessionid):
    """Retrieve a temporary ticket from the reader endpoint."""
    headers = {
        "accept": "application/json",
        "accesskeyid": "123456",
        "appversion": "8.1.0",
        "authorization": f"Bearer {jwt_token}",
        "user-agent": USER_AGENT,
        "websessionid": websessionid,
    }
    response = curl_cffi.get(reader_url, headers=headers, impersonate=TLS_IMPERSONATE)
    response.raise_for_status()
    return response.json()["ticket"]


def fetch_material(ticket):
    """Fetch issue metadata and page/tile information."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": USER_AGENT,
    }
    response = curl_cffi.get(
        f"https://api.cafeyn.co/ticket/{ticket}/material",
        headers=headers,
        impersonate=TLS_IMPERSONATE,
    )
    response.raise_for_status()
    return response.json()


def apply_tile_transformation(image, transformation):
    """Apply Cafeyn tile transform (vertical flip then rotation)."""
    rotation, flip = transformation

    if flip:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    for _ in range(rotation % 4):
        image = image.transpose(Image.Transpose.ROTATE_270)

    return image


def download_and_construct_image(page_data, hosts_base, ticket_value, max_workers):
    """Download all tiles for one page and merge them into a single image."""
    hd = page_data["hd"]
    path = hd["path"]
    tile_col_count = hd["tile_col_count"]
    tile_row_count = hd["tile_row_count"]
    tile_height = hd["tile_height"]
    tile_width = hd["tile_width"]
    page_width = hd.get("width", tile_col_count * tile_width)
    page_height = hd.get("height", tile_row_count * tile_height)
    transformations = hd.get("transformations", {})

    tiles = {}
    total_tiles = tile_col_count * tile_row_count

    def fetch_tile(row, col):
        tile_name = f"tile0{col}x0{row}.jpeg"
        tile_path = path if total_tiles == 1 else f"{path}/{tile_name}"
        dl_url = hosts_base + ticket_value + f"/{tile_path}"
        transformation = transformations.get(tile_name)

        response = curl_cffi.get(dl_url, impersonate=TLS_IMPERSONATE)
        response.raise_for_status()
        tile_image = Image.open(BytesIO(response.content)).convert("RGBA")

        if transformation is not None:
            tile_image = apply_tile_transformation(tile_image, transformation)

        return row, col, tile_image

    workers = min(max_workers, total_tiles) if total_tiles else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(fetch_tile, row, col)
            for row in range(tile_row_count)
            for col in range(tile_col_count)
        ]

        for future in as_completed(futures):
            row, col, tile_image = future.result()
            tiles[(row, col)] = tile_image

    canvas = Image.new("RGBA", (page_width, page_height))

    for row in range(tile_row_count):
        for col in range(tile_col_count):
            tile_image = tiles[(row, col)]
            target_width = min(tile_width, page_width - col * tile_width)
            target_height = min(tile_height, page_height - row * tile_height)

            if tile_image.size != (target_width, target_height):
                tile_image = tile_image.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
            canvas.paste(tile_image, (col * tile_width, row * tile_height))

    return canvas


def build_pdf(material_data, output_dir, ticket, max_workers):
    """Render all pages and write the final PDF to disk."""
    publication_date = material_data["metadata"]["publication_date"]
    publication_title = material_data["metadata"]["title"]
    hosts = material_data["hosts"]["pages"]

    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="cafeyn_pages_"))
    pdf_pages = []

    try:
        page_paths = []
        pages = sorted(material_data["pages"], key=lambda item: item["number"])

        for page in tqdm(pages, desc="Pages", unit="page"):
            number = page["number"]
            page_image = download_and_construct_image(page, hosts, ticket, max_workers)
            page_path = temp_dir / f"page_{number:04d}.png"
            page_image.save(page_path)
            page_paths.append(page_path)

        pdf_name = sanitize_filename(f"{publication_title} - {publication_date}.pdf")
        pdf_path = output_dir / pdf_name

        for page_path in page_paths:
            with Image.open(page_path) as page_image:
                pdf_pages.append(page_image.convert("RGB"))

        if pdf_pages:
            pdf_pages[0].save(pdf_path, save_all=True, append_images=pdf_pages[1:])
            print(f"Saved PDF to: {pdf_path}")
        else:
            print("No pages were returned by the API.")
    finally:
        for image in pdf_pages:
            image.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    """Parse CLI args, load credentials, and run the download pipeline."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Download a Cafeyn issue and export it to PDF."
    )
    parser.add_argument(
        "reader_url",
        nargs="?",
        help="Reader URL, e.g. https://api.cafeyn.co/ticket/reader/21712793/22589611",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where the final PDF is saved (default: output).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Maximum number of parallel tile downloads per page (default: 16).",
    )
    args = parser.parse_args()

    reader_url = (
        args.reader_url
        or input(
            "Reader URL (e.g. https://api.cafeyn.co/ticket/reader/21712793/22589611): "
        ).strip()
    )
    if not reader_url:
        raise ValueError("Reader URL is required.")

    parse_reader_url(reader_url)

    jwt_token = get_env("JWT_TOKEN")
    websessionid = get_env("WEBSESSIONID")

    ticket = fetch_ticket(reader_url, jwt_token, websessionid)
    material_data = fetch_material(ticket)
    build_pdf(
        material_data,
        Path(args.output_dir),
        ticket,
        max_workers=max(1, args.max_workers),
    )


if __name__ == "__main__":
    main()
