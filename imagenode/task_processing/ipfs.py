from dataclasses import dataclass
import requests
from io import BytesIO
import os


@dataclass
class IpfsPinFileRes:
    IpfsHash: str
    PinSize: int
    Timestamp: str
    isDuplicate: bool | None = None


def pin_by_url(url: str, file_name: str, group_id: str | None = None) -> IpfsPinFileRes:
    response = requests.get(url)
    response.raise_for_status()

    file = BytesIO(response.content)

    files = {
        "file": (file_name, file),
    }

    data = {}
    if group_id is not None:
        data["pinataOptions"] = f'{{"groupId": "{group_id}"}}'
    headers = {"Authorization": f"Bearer {os.getenv("PINATA_TOKEN")}"}

    upload_response = requests.post(
        "https://api.pinata.cloud/pinning/pinFileToIPFS",
        headers=headers,
        files=files,
        data=data,
    )
    upload_response.raise_for_status()
    return IpfsPinFileRes(**upload_response.json())
