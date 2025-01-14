from nodetools.models.models import MemoPattern
import re
from imagenode.task_processing.constants import TaskType
from nodetools.configuration.constants import UNIQUE_ID_PATTERN_V1


IMAGE_GEN_PATTERN = MemoPattern(
    memo_type=re.compile(
        f"^{UNIQUE_ID_PATTERN_V1.pattern}__{TaskType.IMAGE_GEN.value}$"
    ),
    memo_data=re.compile(f".*{re.escape(TaskType.IMAGE_GEN.value)}.*"),
)

IMAGE_RESPONSE_PATTERN = MemoPattern(
    memo_type=re.compile(
        f"^{UNIQUE_ID_PATTERN_V1.pattern}__{TaskType.IMAGE_GEN_RESPONSE.value}$"
    ),
    memo_data=re.compile(f".*{re.escape(TaskType.IMAGE_GEN_RESPONSE.value)}.*"),
)
