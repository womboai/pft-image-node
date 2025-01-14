##########################################################################
########################## HELPER FUNCTIONS ##############################
##########################################################################

from nodetools.configuration.constants import UNIQUE_ID_PATTERN_V1
from imagenode.task_processing.patterns import TASK_ID_PATTERN


def is_valid_task_id(memo_type: str) -> bool:
    """Check if a memo type is a valid task ID"""
    return bool(TASK_ID_PATTERN.match(memo_type)) if memo_type else False


def derive_response_memo_type(request_memo_type: str, response_memo_type: str) -> str:
    """
    Derives a response memo_type from a request memo_type.
    Example: "v1.0.2025-01-13_06:53__QQ74__TASK_REQUEST" -> "v1.0.2025-01-13_06:53__QQ74__PROPOSAL"

    Args:
        request_memo_type: Original memo_type from request
        response_type: Type of response (e.g., "PROPOSAL", "VERIFICATION_PROMPT")

    Returns:
        Unique memo_type for the response

    Raises:
        ValueError: If task_id cannot be extracted from request_memo_type
    """
    task_id_match = UNIQUE_ID_PATTERN_V1.search(request_memo_type)
    if not task_id_match:
        raise ValueError(
            f"Could not extract task_id from memo_type: {request_memo_type}"
        )

    task_id = task_id_match.group(1)
    return f"{task_id}__{response_memo_type}"
