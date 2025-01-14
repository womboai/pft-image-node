# Standard library imports
import os
from typing import Dict, Any

# Third-party imports
from loguru import logger

from nodetools.models.models import (
    MemoConstructionParameters,
    MemoTransaction,
    ResponseGenerator,
)

# Task node imports
from imagenode.task_processing.constants import TaskType

# NodeTools imports
from nodetools.configuration.configuration import NodeConfig
from nodetools.protocols.generic_pft_utilities import GenericPFTUtilities

# Custom image generation imports
import fal_client

from imagenode.task_processing.ipfs import pin_by_url
from imagenode.task_processing.utils import derive_response_memo_type


class ImageResponseGenerator(ResponseGenerator):
    def __init__(
        self,
        node_config: NodeConfig,
        generic_pft_utilities: GenericPFTUtilities,
    ):
        self.node_config = node_config
        self.generic_pft_utilities = generic_pft_utilities

    async def evaluate_request(self, request_tx: MemoTransaction) -> Dict[str, Any]:
        """Evaluate image generation request"""
        logger.debug("Evaluating image generation request...")

        # TODO: remove this
        logger.debug(f"RECEIVED MEMO DATA: {request_tx.memo_data}")

        if request_tx.memo_data.strip() == "":
            logger.debug("No memo_data was provided")
            return {"ipfs_hash": None}

        try:
            # Generate image
            result = await fal_client.subscribe_async(
                "fal-ai/flux/dev",
                arguments={
                    "prompt": request_tx.memo_data,
                    "image_size": "square",
                    "num_images": 1,
                },
            )

            image_url = result["images"][0]["url"]

            # Pin image to IPFS
            data = pin_by_url(
                image_url,
                request_tx.get("hash") or "unknown_tx",
                os.getenv("PINATA_GROUP_ID"),
            )
            ipfs_hash = data["IpfsHash"]

            return {
                "ipfs_hash": ipfs_hash,
            }
        except Exception as e:
            logger.error(f"Failed to generate image with error: {e}")
            return {"ipfs_hash": None}

    async def construct_response(
        self, request_tx: MemoTransaction, evaluation_result: Dict[str, Any]
    ) -> MemoConstructionParameters:
        """Construct image response parameters"""

        logger.debug("Constructing image generation response...")
        try:
            ipfs_hash = evaluation_result["ipfs_hash"]

            if ipfs_hash is None:
                raise Exception("ipfs hash from evaluating request was null")

            logger.debug(f"Constructing response with ipfs hash: {ipfs_hash}")

            response_string = "ipfs hash: " + ipfs_hash

            logger.debug(f"Constructed response string: {response_string}")

            response_memo_type = derive_response_memo_type(
                request_memo_type=request_tx.memo_type,
                response_memo_type=TaskType.IMAGE_GEN_RESPONSE.value,
            )

            memo = MemoConstructionParameters.construct_standardized_memo(
                source=self.node_config.node_name,
                destination=request_tx.account,
                memo_data=response_string,
                memo_type=response_memo_type,
            )

            return memo
        except Exception as e:
            raise Exception(f"Failed to construct image generation response: {e}")
