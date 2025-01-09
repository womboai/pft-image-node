"""
This module defines the business logic rules for processing imagenode XRPL transactions.
There are two distinct layers of validation:

1. Pattern Matching (handled by TransactionGraph):
   - Validates the structure of memos (memo_type, memo_data, and memo_format)
   - Matches transactions to their correct workflow type
   - Determines valid response patterns for request transactions
   - Example: Checking if a memo matches the pattern for a proposal (PROPOSED PF ___)

2. Business Rule Validation (handled by Rule classes):
   - Enforces content-specific rules (e.g., minimum length for initiation rites)
   - Checking that a transaction is addressed to the node address or remembrancer address
   - Example: Checking if an initiation rite contains meaningful content

Example Flow:
1. Transaction received: memo_type="2024-03-20_14:30", memo_data="REQUEST_POST_FIAT ___ Can i get a task to do?"
2. TransactionGraph matches this to the "request_post_fiat" pattern
3. RequestPostFiatRule then used to validate that the transaction was addressed to the node
4. After validation, node will check against its database to see if the request already has a valid response, 
    according to the valid_responses set in the TransactionGraph
5. If no valid response is found, node will conclude that the request is unfulfilled and will call the response rule's generator
6. The response rule's generator will generate a response
7. The node will send the response back to the user and mark it for re-review
8. The node will queue the response for re-review and confirm that the response was sent
9. The node will then mark the request as fulfilled
10. The interaction is complete

When adding new rules, remember:
- A request requires a single valid response 
- Pattern matching logic belongs in create_business_logic()
- Only transaction-specific validation logic belongs in InteractionRule.validate()
- NodeTools ignores failed transactions by default, so explicitly checking for transaction success is not necessary
"""

from nodetools.configuration.constants import SystemMemoType
from nodetools.models.models import (
    InteractionGraph,
    MemoPattern,
    BusinessLogicProvider,
    InteractionType,
)

# Task node imports
from imagenode.task_processing.image_gen.patterns import (
    IMAGE_GEN_PATTERN,
    IMAGE_RESPONSE_PATTERN,
)
from imagenode.task_processing.image_gen.rules import ImageGenResponseRule, ImageGenRule

##############################################################################
############################## MEMO PATTERNS #################################
##############################################################################

# System memo patterns
INITIATION_RITE_PATTERN = MemoPattern(memo_type=SystemMemoType.INITIATION_RITE.value)
INITIATION_REWARD_PATTERN = MemoPattern(
    memo_type=SystemMemoType.INITIATION_REWARD.value
)
HANDSHAKE_PATTERN = MemoPattern(memo_type=SystemMemoType.HANDSHAKE.value)
GOOGLE_DOC_LINK_PATTERN = MemoPattern(
    memo_type=SystemMemoType.GOOGLE_DOC_CONTEXT_LINK.value
)


##########################################################################
####################### BUSINESS LOGIC PROVIDER ##########################
##########################################################################


class TaskManagementRules(BusinessLogicProvider):
    """Business logic for task management"""

    @classmethod
    def create(cls) -> "TaskManagementRules":
        """Factory function to create all business logic components"""
        # Setup transaction graph
        graph = InteractionGraph()

        # Create rules so we can map them to patterns
        rules = {
            # "google_doc_link": GoogleDocLinkRule(),
            # "handshake_request": HandshakeRequestRule(),
            # "handshake_response": HandshakeResponseRule(),
            "image_gen": ImageGenRule(),
            "image_gen_response": ImageGenResponseRule(),
        }

        # # Add google doc link patterns to graph
        # graph.add_pattern(
        #     pattern_id="google_doc_link",
        #     memo_pattern=GOOGLE_DOC_LINK_PATTERN,
        #     transaction_type=InteractionType.STANDALONE,
        #     notify=True
        # )
        #
        # # Add handshake patterns to graph
        # graph.add_pattern(
        #     pattern_id="handshake_request",
        #     memo_pattern=HANDSHAKE_PATTERN,
        #     transaction_type=InteractionType.REQUEST,
        #     valid_responses={HANDSHAKE_PATTERN},
        # )
        # graph.add_pattern(
        #     pattern_id="handshake_response",
        #     memo_pattern=HANDSHAKE_PATTERN,
        #     transaction_type=InteractionType.RESPONSE,
        # )

        # Add image generation patterns to graph
        graph.add_pattern(
            pattern_id="image_gen",
            memo_pattern=IMAGE_GEN_PATTERN,
            transaction_type=InteractionType.REQUEST,
            valid_responses={IMAGE_RESPONSE_PATTERN},
            notify=True,
        )

        graph.add_pattern(
            pattern_id="image_gen_response",
            memo_pattern=IMAGE_RESPONSE_PATTERN,
            transaction_type=InteractionType.RESPONSE,
            notify=True,
        )

        return cls(transaction_graph=graph, pattern_rule_map=rules)


##########################################################################
########################## Google Doc Link ###############################
##########################################################################

# class GoogleDocLinkRule(StandaloneRule):
#     """
#     Pure business logic for handling google doc links
#     Currently, this rule is a placeholder and does not perform any validation.
#     """
#     async def validate(self, *args, **kwargs) -> bool:
#         return True

##########################################################################
########################## HANDSHAKE RULES ###############################
##########################################################################

# class HandshakeRequestRule(RequestRule):
#     """Pure business logic for handling handshake requests"""
#
#     async def validate(
#             self,
#             tx: Dict[str, Any],
#             dependencies: Dependencies
#         ) -> bool:
#         """
#         Validate business rules for a handshake request.
#         Pattern matching is handled by TransactionGraph.
#         Must:
#         1. Be sent an address in the node's auto-handshake addresses
#         2. Be a valid ECDH public key
#         3. Be a verified address associated with an active Discord user
#         """
#         if tx.get('destination') not in dependencies.node_config.auto_handshake_addresses:
#             return False
#
#         if REQUIRE_AUTHORIZATION:
#             is_authorized = await dependencies.transaction_repository.is_address_authorized(
#                 tx.get('account')
#             )
#             if not is_authorized:
#                 # logger.debug(f"HandshakeRequestRule.validate: Address {tx.get('account')} is not authorized")
#                 return False
#
#         try:
#             # Determine which secret type to use based on receiving address
#             secret_type = SecretType.NODE if tx['destination'] == dependencies.node_config.node_address else SecretType.REMEMBRANCER
#
#             # Try to derive shared secret - this will fail if the public key is invalid
#             received_key = tx.get('memo_data', '')
#             dependencies.credential_manager.get_shared_secret(
#                 received_key=received_key,
#                 secret_type=secret_type
#             )
#             return True
#
#         except Exception as e:
#             return False
#
#     async def find_response(
#             self,
#             request_tx: Dict[str, Any],
#         ) -> Optional[ResponseQuery]:
#         """
#         Get query information for finding a handshake response.
#         The response must be:
#         1. Sent to the same account
#         2. Sent from the account that received the handshake request
#         3. Have HANDSHAKE memo type
#         4. Successful transaction (handled by find_transaction_response)
#         """
#         query = """
#             SELECT * FROM find_transaction_response(
#                 request_account := %(account)s,
#                 request_destination := %(destination)s,
#                 request_time := %(request_time)s,
#                 response_memo_type := %(response_memo_type)s,
#                 require_after_request := TRUE  -- Check for ANY existing response
#             );
#         """
#
#         params = {
#             # Attempt to retrieve account and destination from top level of tx or tx_json_parsed
#             'account': request_tx['account'],
#             'destination': request_tx['destination'],
#             'request_time': request_tx['close_time_iso'],
#             'response_memo_type': SystemMemoType.HANDSHAKE.value
#         }
#
#         return ResponseQuery(query=query, params=params)
#
# class HandshakeResponseRule(ResponseRule):
#     """Pure business logic for handling handshake responses"""
#
#     async def validate(self, *args, **kwargs) -> bool:
#         return True
#
#     def get_response_generator(self, dependencies: Dependencies) -> ResponseGenerator:
#         """Get response generator for handshake response with all dependencies"""
#         return HandshakeResponseGenerator(
#             node_config=dependencies.node_config,
#             generic_pft_utilities=dependencies.generic_pft_utilities,
#             cred_manager=dependencies.credential_manager
#         )
#
# class HandshakeResponseGenerator(ResponseGenerator):
#     """Evaluates handshake requests and generates response parameters."""
#     def __init__(
#             self,
#             node_config: NodeConfig,
#             generic_pft_utilities: GenericPFTUtilities,
#             cred_manager: CredentialManager
#         ):
#         self.node_config = node_config
#         self.generic_pft_utilities = generic_pft_utilities
#         self.cred_manager = cred_manager
#
#     def _determine_secret_type(self, address: str) -> SecretType:
#         """Determines SecretType based on address"""
#         if address == self.node_config.node_address:
#             return SecretType.NODE
#         elif address == self.node_config.remembrancer_address:
#             return SecretType.REMEMBRANCER
#         else:
#             raise ValueError(f"No SecretType found for address: {address}")
#
#     def _get_source_name(self, secret_type: SecretType) -> str:
#         """Returns the appropriate source name based on SecretType"""
#         match secret_type:
#             case SecretType.NODE:
#                 return self.node_config.node_name
#             case SecretType.REMEMBRANCER:
#                 return self.node_config.remembrancer_name
#
#     async def evaluate_request(self, request_tx: Dict[str, Any]) -> Dict[str, Any]:
#         """Evaluate handshake request and determine response parameters"""
#         destination_address = request_tx['account']
#         request_destination = request_tx['destination']  # The node address that received the request
#
#         # Determine SecretType for ECDH key retrieval
#         secret_type = self._determine_secret_type(request_destination)
#
#         # Get ECDH public key for the responding node address
#         ecdh_key = self.cred_manager.get_ecdh_public_key(secret_type)
#
#         return {
#             'destination': destination_address,
#             'ecdh_key': ecdh_key,
#             'source': request_destination,
#             'secret_type': secret_type
#         }
#
#     async def construct_response(
#             self,
#             request_tx: Dict[str, Any],
#             evaluation_result: Dict[str, Any]
#         ) -> Dict[str, Any]:
#         """Construct handshake response parameters"""
#         try:
#             # Get the appropriate source name
#             source_name = self._get_source_name(evaluation_result['secret_type'])
#
#             # Construct handshake memo
#             memo = self.generic_pft_utilities.construct_handshake_memo(
#                 user=evaluation_result['destination'],
#                 ecdh_public_key=evaluation_result['ecdh_key']
#             )
#
#             return ResponseParameters(
#                 source=source_name,
#                 memo=memo,
#                 destination=evaluation_result['destination'],
#                 pft_amount=None  # No PFT amount for handshake responses
#             )
#
#         except Exception as e:
#             raise Exception(f"Failed to construct handshake response: {e}")
