# Standard library imports
from typing import Optional

# NodeTools imports
from nodetools.models.models import (
    MemoTransaction,
    ResponseQuery,
    RequestRule,
    ResponseRule,
    ResponseGenerator,
    Dependencies,
    ValidationResult,
)

from imagenode.task_processing.constants import IMAGE_GEN_COST, TaskType
from imagenode.task_processing.image_gen.response import ImageResponseGenerator
from imagenode.task_processing.utils import derive_response_memo_type


class ImageGenRule(RequestRule):
    """Pure business logic for queuing image generation tasks."""

    async def validate(
        self, tx: MemoTransaction, dependencies: Dependencies
    ) -> ValidationResult:
        """
        Validate business rules for a image generation request.
        Pattern matching is handled by TransactionGraph.
        Must:
        1. Be addressed to the node address
        2. Must have sent 1 PFT
        """
        if tx.destination != dependencies.node_config.node_address:
            return ValidationResult(
                valid=False, notes=f"wrong destination address {tx.destination}"
            )
        if tx.pft_amount < IMAGE_GEN_COST:
            return ValidationResult(
                valid=False, notes=f"insufficient PFT amount: {tx.pft_amount}"
            )

        return ValidationResult(valid=True)

    async def find_response(
        self,
        request_tx: MemoTransaction,
    ) -> Optional[ResponseQuery]:
        """Get query information for finding a image generation response."""
        query = """
            SELECT * FROM find_transaction_response(
                request_account := %(account)s,
                request_destination := %(destination)s,
                request_time := %(request_time)s,
                response_memo_type := %(response_memo_type)s,
                require_after_request := TRUE
            );
        """

        response_memo_type = derive_response_memo_type(
            request_memo_type=request_tx.memo_type,
            response_memo_type=TaskType.IMAGE_GEN_RESPONSE.value,
        )
        # NOTE: look for image responses by the node that match the given request
        params = {
            "account": request_tx.account,
            "destination": request_tx.destination,
            "request_time": request_tx.datetime,
            "response_memo_type": response_memo_type,
        }

        return ResponseQuery(query=query, params=params)


class ImageGenResponseRule(ResponseRule):
    """Pure business logic for handling returning generated images"""

    async def validate(
        self, tx: MemoTransaction, dependencies: Dependencies
    ) -> ValidationResult:
        return ValidationResult(valid=True)

    def get_response_generator(self, dependencies: Dependencies) -> ResponseGenerator:
        """Get response generator for images with dependencies"""
        return ImageResponseGenerator(
            node_config=dependencies.node_config,
            generic_pft_utilities=dependencies.generic_pft_utilities,
        )
