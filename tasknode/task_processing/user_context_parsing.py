import pandas as pd
from nodetools.protocols.generic_pft_utilities import GenericPFTUtilities
import tasknode.task_processing.constants as node_constants
from nodetools.configuration.constants import SystemMemoType
from nodetools.configuration.configuration import NodeConfig
from nodetools.protocols.credentials import CredentialManager
from typing import Optional, Union, TYPE_CHECKING
from loguru import logger
import traceback
import re
from tasknode.task_processing.constants import TaskType, TASK_PATTERNS
import requests

if TYPE_CHECKING:
    from tasknode.task_processing.tasknode_utilities import TaskNodeUtilities

class UserTaskParser:
    _instance = None
    _initialized = False

    STATE_COLUMN_MAP = {
        TaskType.ACCEPTANCE: 'acceptance',
        TaskType.REFUSAL: 'refusal',
        TaskType.VERIFICATION_PROMPT: 'verification',
        TaskType.REWARD: 'reward'
    }

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
            self,
            generic_pft_utilities: GenericPFTUtilities,
            node_config: NodeConfig,
            credential_manager: CredentialManager
        ):
        """Initialize UserTaskParser with GenericPFTUtilities for core functionality"""
        if not self.__class__._initialized:
            self.generic_pft_utilities = generic_pft_utilities
            self.node_config = node_config
            self.cred_manager = credential_manager
            self.__class__._initialized = True

    def classify_task_string(self, string: str) -> str:
        """Classifies a task string using TaskType enum patterns.
        
        Args:
            string: The string to classify
            
        Returns:
            str: The name of the task type or 'UNKNOWN'
        """

        for task_type, patterns in TASK_PATTERNS.items():
            if any(pattern in string for pattern in patterns):
                return task_type.name

        return 'UNKNOWN'

    @staticmethod
    def is_valid_id(memo_type: str) -> bool:
        """Check if memo_type contains a valid task ID in format YYYY-MM-DD_HH:MM or YYYY-MM-DD_HH:MM__XXXX.
        
        Args:
            memo_type: The memo_type string to check for task ID pattern
            
        Returns:
            bool: True if the memo_type contains a valid task ID pattern
        """
        if not memo_type:
            return False
        task_id_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}(?:__[A-Z0-9]{4})?)')
        return bool(re.search(task_id_pattern, str(memo_type)))
    
    def filter_tasks(self, account_memo_detail_df):
        """Filter account transaction history to only include tasks"""
        # Return immediately if no tasks found
        if account_memo_detail_df.empty:
            return pd.DataFrame()

        simplified_task_frame = account_memo_detail_df[
            account_memo_detail_df['memo_type'].apply(self.is_valid_id)
        ].copy()

        # Return immediately if no tasks found
        if simplified_task_frame.empty:
            return pd.DataFrame()

        simplified_task_frame['task_type'] = simplified_task_frame['memo_data'].apply(self.classify_task_string)

        return simplified_task_frame

    def get_task_state_pairs(self, account_memo_detail_df):
        """Convert account info into a DataFrame of proposed tasks and their latest state changes."""
        task_frame = self.filter_tasks(
            account_memo_detail_df=account_memo_detail_df.sort_values('datetime')
        )

        if task_frame.empty:
            return pd.DataFrame()

        # Rename columns for clarity
        task_frame.rename(columns={
            'memo_type': 'task_id',
            'memo_data': 'full_output',
            'memo_format': 'user_account'
        }, inplace=True)

        # Get proposals
        proposals = task_frame[
            task_frame['task_type']==TaskType.PROPOSAL.name
        ].groupby('task_id').first()['full_output']

        # Get latest state changes
        state_changes = task_frame[
            (task_frame['task_type'].isin([
                TaskType.ACCEPTANCE.name,
                TaskType.REFUSAL.name,
                TaskType.VERIFICATION_PROMPT.name,
                TaskType.REWARD.name
            ]))
        ].groupby('task_id').last()[['full_output','task_type', 'datetime']]

        # Start with all proposals
        task_pairs = pd.DataFrame({'proposal': proposals})

        # For each task id, if there's no state change, it's in PROPOSAL state
        all_task_ids = task_pairs.index
        task_pairs['state_type'] = pd.Series(
            TaskType.PROPOSAL.name, 
            index=all_task_ids
        )

        # Update state types and other fields where we have state changes
        # Only update states for task IDs that exist in both DataFrames
        common_ids = state_changes.index.intersection(task_pairs.index)
        task_pairs.loc[common_ids, 'state_type'] = state_changes.loc[common_ids, 'task_type']
        task_pairs.loc[common_ids, 'latest_state'] = state_changes.loc[common_ids, 'full_output']
        task_pairs.loc[common_ids, 'datetime'] = state_changes.loc[common_ids, 'datetime']

        # Handle orphaned state changes (states without corresponding proposals)
        orphaned_states = state_changes.index.difference(task_pairs.index)
        if not orphaned_states.empty:
            logger.warning(f"Found {len(orphaned_states)} state changes without corresponding proposals. "
                        f"This may indicate incomplete history. Task IDs: {orphaned_states.tolist()}")
            
            # Create entries for orphaned states with empty proposals
            orphaned_df = pd.DataFrame(index=orphaned_states)
            orphaned_df['proposal'] = ''
            orphaned_df['state_type'] = state_changes.loc[orphaned_states, 'task_type']
            orphaned_df['latest_state'] = state_changes.loc[orphaned_states, 'full_output']
            orphaned_df['datetime'] = state_changes.loc[orphaned_states, 'datetime']
            
            # Combine with main DataFrame
            task_pairs = pd.concat([task_pairs, orphaned_df])
        
        # Fill any missing values
        task_pairs['latest_state'] = task_pairs['latest_state'].fillna('')
        task_pairs['datetime'] = task_pairs['datetime'].fillna(pd.NaT)

        return task_pairs

    async def get_proposals_by_state(
            self, 
            account: Union[str, pd.DataFrame], 
            state_type: TaskType
        ):
        """Get proposals filtered by their state."""
        # Handle input type
        if isinstance(account, str):
            account_memo_detail_df = await self.generic_pft_utilities.get_account_memo_history(account_address=account)
        else:
            account_memo_detail_df = account

        # Get base task pairs
        task_pairs = self.get_task_state_pairs(account_memo_detail_df)

        if task_pairs.empty:
            return pd.DataFrame()

        if state_type == TaskType.PROPOSAL:
            # Handle pending proposals
            filtered_proposals = task_pairs[
                task_pairs['state_type'] == TaskType.PROPOSAL.name
            ][['proposal']]

            filtered_proposals['proposal'] = filtered_proposals['proposal'].apply(
                lambda x: str(x).replace(TaskType.PROPOSAL.value, '').replace('nan', '')
            )

            return filtered_proposals
        
        # Filter to requested state
        filtered_proposals = task_pairs[
            task_pairs['state_type'] == state_type.name
        ][['proposal', 'latest_state']].copy()
        
        # Clean up text content
        filtered_proposals['latest_state'] = filtered_proposals['latest_state'].apply(
            lambda x: str(x).replace(state_type.value, '').replace('nan', '')
        )
        filtered_proposals['proposal'] = filtered_proposals['proposal'].apply(
            lambda x: str(x).replace(TaskType.PROPOSAL.value, '').replace('nan', '')
        )

        return filtered_proposals
    
    async def get_pending_proposals(self, account: Union[str, pd.DataFrame]):
        """Get proposals that have not yet been accepted or refused."""
        return await self.get_proposals_by_state(account, state_type=TaskType.PROPOSAL)

    async def get_accepted_proposals(self, account: Union[str, pd.DataFrame]):
        """Get accepted proposals"""
        proposals = await self.get_proposals_by_state(account, state_type=TaskType.ACCEPTANCE)
        if not proposals.empty:
            proposals.rename(columns={'latest_state': self.STATE_COLUMN_MAP[TaskType.ACCEPTANCE]}, inplace=True)
        return proposals
    
    async def get_verification_proposals(self, account: Union[str, pd.DataFrame]):
        """Get verification proposals"""
        proposals = await self.get_proposals_by_state(account, state_type=TaskType.VERIFICATION_PROMPT)
        if not proposals.empty:
            proposals.rename(columns={'latest_state': self.STATE_COLUMN_MAP[TaskType.VERIFICATION_PROMPT]}, inplace=True)
        return proposals

    async def get_rewarded_proposals(self, account: Union[str, pd.DataFrame]):
        """Get rewarded proposals"""
        proposals = await self.get_proposals_by_state(account, state_type=TaskType.REWARD)
        if not proposals.empty:
            proposals.rename(columns={'latest_state': self.STATE_COLUMN_MAP[TaskType.REWARD]}, inplace=True)
        return proposals

    async def get_refused_proposals(self, account: Union[str, pd.DataFrame]):
        """Get refused proposals"""
        proposals = await self.get_proposals_by_state(account, state_type=TaskType.REFUSAL)
        if not proposals.empty:
            proposals.rename(columns={'latest_state': self.STATE_COLUMN_MAP[TaskType.REFUSAL]}, inplace=True)
        return proposals
    
    async def get_refuseable_proposals(self, account: Union[str, pd.DataFrame]):
        """Get all proposals that are in a valid state to be refused.
        
        This includes:
        - Pending proposals
        - Accepted proposals
        - Verification proposals
        
        Does not include proposals that have already been refused or rewarded.
        
        Args:
            account: Either an XRPL account address string or a DataFrame containing memo history.
                
        Returns:
            DataFrame with columns:
                - proposal: The proposed task text
            Indexed by task_id.
        """
        # Get all proposals in refuseable states
        pending = await self.get_proposals_by_state(account, state_type=TaskType.PROPOSAL)
        accepted = await self.get_proposals_by_state(account, state_type=TaskType.ACCEPTANCE)
        verification = await self.get_proposals_by_state(account, state_type=TaskType.VERIFICATION_PROMPT)

        if pending.empty and accepted.empty and verification.empty:
            return pd.DataFrame()
        
        # Combine all proposals, keeping only the proposal text column
        all_proposals = pd.concat([
            pending[['proposal']],
            accepted[['proposal']],
            verification[['proposal']]
        ])
        
        return all_proposals.drop_duplicates()

    async def get_task_statistics(self, account_address):
        """
        Get statistics about user's tasks.
        
        Args:
            account_address: XRPL account address to get stats for
            
        Returns:
            dict containing:
                - total_tasks: Total number of tasks
                - accepted_tasks: Number of accepted tasks
                - pending_tasks: Number of pending tasks
                - acceptance_rate: Percentage of tasks accepted
        """
        account_memo_detail_df = await self.generic_pft_utilities.get_account_memo_history(account_address)

        pending_proposals = await self.get_pending_proposals(account_memo_detail_df)
        accepted_proposals = await self.get_accepted_proposals(account_memo_detail_df)
        refused_proposals = await self.get_refused_proposals(account_memo_detail_df)
        verification_proposals = await self.get_verification_proposals(account_memo_detail_df)
        rewarded_proposals = await self.get_rewarded_proposals(account_memo_detail_df)

        # Calculate total accepted tasks
        total_accepted = len(accepted_proposals) + len(verification_proposals) + len(rewarded_proposals)

        # Total tasks excluding pending
        total_ended_tasks = total_accepted + len(refused_proposals)

        # Total tasks
        total_tasks = total_ended_tasks + len(pending_proposals)
            
        # Calculate rates
        acceptance_rate = (total_accepted / total_tasks * 100) if total_tasks > 0 else 0
        completion_rate = (len(rewarded_proposals) / total_ended_tasks * 100) if total_ended_tasks > 0 else 0
        
        return {
            'total_tasks': total_tasks,
            'total_ended_tasks': total_ended_tasks,
            'total_completed_tasks': len(rewarded_proposals),
            'total_pending_tasks': len(pending_proposals),
            'acceptance_rate': acceptance_rate,
            'completion_rate': completion_rate
        }

    async def get_full_user_context_string(
        self,
        account_address: str,
        memo_history: Optional[pd.DataFrame] = None,
        get_google_doc: bool = True,
        get_historical_memos: bool = True,
        n_memos_in_context: int = node_constants.MAX_CHUNK_MESSAGES_IN_CONTEXT,
        n_pending_proposals_in_context: int = node_constants.MAX_PENDING_PROPOSALS_IN_CONTEXT,
        n_acceptances_in_context: int = node_constants.MAX_ACCEPTANCES_IN_CONTEXT,
        n_verification_in_context: int = node_constants.MAX_ACCEPTANCES_IN_CONTEXT,
        n_rewards_in_context: int = node_constants.MAX_REWARDS_IN_CONTEXT,
        n_refusals_in_context: int = node_constants.MAX_REFUSALS_IN_CONTEXT,
    ) -> str:
        """Get complete user context including task states and optional content.
        
        Args:
            account_address: XRPL account address
            memo_history: Optional pre-fetched memo history DataFrame to avoid requerying
            get_google_doc: Whether to fetch Google doc content
            get_historical_memos: Whether to fetch historical memos
            n_task_context_history: Number of historical items to include
        """
        # Use provided memo_history or fetch if not provided
        if memo_history is None:
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=account_address)

        # Handle proposals section (pending + accepted)
        try:
            pending_proposals = await self.get_pending_proposals(memo_history)
            accepted_proposals = await self.get_accepted_proposals(memo_history)

            # Combine and limit
            all_proposals = pd.concat([pending_proposals, accepted_proposals]).tail(
                n_acceptances_in_context + n_pending_proposals_in_context
            )

            if all_proposals.empty:
                proposal_string = "No pending or accepted proposals found."
            else:
                proposal_string = self.format_task_section(all_proposals, TaskType.PROPOSAL)
        
        except Exception as e:
            logger.error(f"UserTaskParser.get_full_user_context_string: Failed to get pending or accepted proposals: {e}")
            logger.error(traceback.format_exc())
            proposal_string = "Error retrieving pending or accepted proposals."

        # Handle refusals
        try:
            refused_proposals = await self.get_refused_proposals(memo_history)
            refused_proposals = refused_proposals.tail(n_refusals_in_context)
            if refused_proposals.empty:
                refusal_string = "No refused proposals found."
            else:
                refusal_string = self.format_task_section(refused_proposals, TaskType.REFUSAL)
        except Exception as e:
            logger.error(f"UserTaskParser.get_full_user_context_string: Failed to get refused proposals: {e}")
            logger.error(traceback.format_exc())
            refusal_string = "Error retrieving refused proposals."
            
        # Handle verifications
        try:
            verification_proposals = await self.get_verification_proposals(memo_history)
            verification_proposals = verification_proposals.tail(n_verification_in_context)
            if verification_proposals.empty:
                verification_string = "No tasks pending verification."
            else:
                verification_string = self.format_task_section(verification_proposals, TaskType.VERIFICATION_PROMPT)
        except Exception as e:
            logger.error(f'UserTaskParser.get_full_user_context_string: Exception while retrieving verifications for {account_address}: {e}')
            logger.error(traceback.format_exc())
            verification_string = "Error retrieving verifications."    

        # Handle rewards
        try:
            rewarded_proposals = await self.get_rewarded_proposals(memo_history)
            rewarded_proposals = rewarded_proposals.tail(n_rewards_in_context)
            if rewarded_proposals.empty:
                reward_string = "No rewarded tasks found."
            else:
                reward_string = self.format_task_section(rewarded_proposals, TaskType.REWARD)
        except Exception as e:
            logger.error(f'UserTaskParser.get_full_user_context_string: Exception while retrieving rewards for {account_address}: {e}')
            logger.error(traceback.format_exc())
            reward_string = "Error retrieving rewards."

        # Get optional context elements
        if get_google_doc:
            try:
                google_url = await self.get_latest_outgoing_context_doc_link(account_address=account_address)
                core_element__google_doc_text = await self.get_google_doc_text(google_url)
            except Exception as e:
                logger.error(f"Failed retrieving user google doc: {e}")
                logger.error(traceback.format_exc())
                core_element__google_doc_text = 'Error retrieving google doc'

        if get_historical_memos:
            try:
                core_element__user_log_history = await self.generic_pft_utilities.get_recent_user_memos(
                    account_address=account_address,
                    num_messages=n_memos_in_context
                )
            except Exception as e:
                logger.error(f"Failed retrieving user memo history: {e}")
                logger.error(traceback.format_exc())
                core_element__user_log_history = 'Error retrieving user memo history'

        core_elements = f"""
***<<< ALL TASK GENERATION CONTEXT STARTS HERE >>>***

These are the proposed and accepted tasks that the user has. This is their
current work queue
<<PROPOSED AND ACCEPTED TASKS START HERE>>
{proposal_string}
<<PROPOSED AND ACCEPTED TASKS ENDE HERE>>

These are the tasks that the user has been proposed and has refused.
The user has provided a refusal reason with each one. Only their most recent
{n_refusals_in_context} refused tasks are showing 
<<REFUSED TASKS START HERE >>
{refusal_string}
<<REFUSED TASKS END HERE>>

These are the tasks that the user has for pending verification.
They need to submit details
<<VERIFICATION TASKS START HERE>>
{verification_string}
<<VERIFICATION TASKS END HERE>>

<<REWARDED TASKS START HERE >>
{reward_string}
<<REWARDED TASKS END HERE >>
"""

        optional_elements = ''
        if get_google_doc:
            optional_elements += f"""
The following is the user's full planning document that they have assembled
to inform task generation and planning
<<USER PLANNING DOC STARTS HERE>>
{core_element__google_doc_text}
<<USER PLANNING DOC ENDS HERE>>
"""

        if get_historical_memos:
            optional_elements += f"""
The following is the users own comments regarding everything
<<< USER COMMENTS AND LOGS START HERE>>
{core_element__user_log_history}
<<< USER COMMENTS AND LOGS END HERE>>
"""

        footer = f"""
***<<< ALL TASK GENERATION CONTEXT ENDS HERE >>>***
"""

        return core_elements + optional_elements + footer
    
    def format_task_section(self, task_df: pd.DataFrame, state_type: TaskType) -> str:
        """Format tasks for display based on their state type.
        
        Args:
            task_df: DataFrame containing tasks with columns:
                - proposal: The proposed task text
                - acceptance/refusal/verification/reward: The state-specific text
                - datetime: Optional timestamp of state change
            state_type: TaskType enum indicating the state to format for
            
        Returns:
            Formatted string representation with columns:
                - initial_task_detail: Original proposal
                - recent_status: State-specific text or status
                - recent_date: From datetime if available, otherwise from task_id
        """
        if task_df.empty:
            return f"No {state_type.name.lower()} tasks found."

        formatted_df = pd.DataFrame(index=task_df.index)
        formatted_df['initial_task_detail'] = task_df['proposal']

        # Use actual datetime if available, otherwise extract from task_id
        if 'datetime' in task_df.columns:
            formatted_df['recent_date'] = task_df['datetime'].dt.strftime('%Y-%m-%d')
        else:
            formatted_df['recent_date'] = task_df.index.map(
                lambda x: x.split('_')[0] if '_' in x else ''
            )

        # Map state types to their column names and expected status text
        state_column_map = {
            TaskType.PROPOSAL: ('acceptance', lambda x: x if pd.notna(x) and str(x).strip() else "Pending response"),
            TaskType.ACCEPTANCE: ('acceptance', lambda x: x),
            TaskType.REFUSAL: ('refusal', lambda x: x),
            TaskType.VERIFICATION_PROMPT: ('verification', lambda x: x),
            TaskType.REWARD: ('reward', lambda x: x)
        }
        
        column_name, status_formatter = state_column_map[state_type]
        if column_name in task_df.columns:
            formatted_df['recent_status'] = task_df[column_name].apply(status_formatter)
        else:
            formatted_df['recent_status'] = "Status not available"
        
        return formatted_df[['initial_task_detail', 'recent_status', 'recent_date']].to_string()
    
    async def get_latest_outgoing_context_doc_link(
            self, 
            account_address: str
        ) -> Optional[str]:
        """Get the most recent Google Doc context link sent by this wallet.
        Handles both encrypted and unencrypted links for backwards compatibility.
            
        Args:
            account_address: Account address
            
        Returns:
            str or None: Most recent Google Doc link or None if not found
        """
        try:
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=account_address, pft_only=False)

            if memo_history.empty or len(memo_history) == 0:
                logger.debug(f"UserTaskParser.get_latest_outgoing_context_doc_link: No memo history found for {account_address}. Returning None")
                return None

            context_docs = memo_history[
                (memo_history['memo_type'].apply(lambda x: SystemMemoType.GOOGLE_DOC_CONTEXT_LINK.value in str(x))) &
                (memo_history['account'] == account_address) &
                (memo_history['transaction_result'] == "tesSUCCESS")
            ]
            
            if len(context_docs) > 0:
                latest_doc = context_docs.iloc[-1]
                
                return await self.generic_pft_utilities.process_memo_data(
                    memo_type=latest_doc['memo_type'],
                    memo_data=latest_doc['memo_data'],
                    channel_address=self.node_config.node_address,
                    channel_counterparty=account_address,
                    memo_history=memo_history,
                    channel_private_key=self.cred_manager.get_credential(f"{self.node_config.node_name}__v1xrpsecret")
                )
            else:
                logger.debug(f"UserTaskParser.get_latest_outgoing_context_doc_link: No context doc found for {account_address}. Returning None")

            return None
            
        except Exception as e:
            logger.error(f"UserTaskParser.get_latest_outgoing_context_doc_link: Error getting latest context doc link: {e}")
            return None

    @staticmethod
    async def get_google_doc_text(share_link):
        """Get the plain text content of a Google Doc.
        
        Args:
            share_link: Google Doc share link
            
        Returns:
            str: Plain text content of the Google Doc
        """
        logger.debug(f"UserTaskParser.get_google_doc_text: Getting Google Doc text for {share_link}")
        # Extract the document ID from the share link
        doc_id = share_link.split('/')[5]
    
        # Construct the Google Docs API URL
        url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    
        # Send a GET request to the API URL
        response = requests.get(url)
    
        # Check if the request was successful
        if response.status_code == 200:
            # Return the plain text content of the document
            return response.text
        else:
            # Return an error message if the request was unsuccessful
            # DON'T CHANGE THIS STRING, IT'S USED FOR GOOGLE DOC VALIDATION
            return f"Failed to retrieve the document. Status code: {response.status_code}"