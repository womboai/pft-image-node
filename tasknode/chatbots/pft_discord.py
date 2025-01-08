# standard imports
import asyncio
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass
import traceback
import getpass
import pytz
import sys
from typing import Optional, Dict, Any
import signal
import re
from decimal import Decimal

# third party imports
from xrpl.wallet import Wallet
import discord
from discord import Object, Interaction, SelectOption, app_commands
from discord.ui import View, Select, Button
from loguru import logger
import pandas as pd

# nodetools imports
import nodetools.configuration.constants as global_constants
from nodetools.configuration.configuration import (
    RuntimeConfig, 
    NetworkConfig, 
    NodeConfig,
)
from nodetools.configuration.configure_logger import configure_logger
from nodetools.ai.openrouter import OpenRouterTool
from nodetools.ai.openai import OpenAIRequestTool
from nodetools.utilities.credentials import CredentialManager
from nodetools.utilities.generic_pft_utilities import GenericPFTUtilities
from nodetools.utilities.transaction_repository import TransactionRepository
from nodetools.performance.monitor import PerformanceMonitor
from nodetools.container.service_container import ServiceContainer

# tasknode imports
from tasknode.task_processing.tasknode_utilities import TaskNodeUtilities
from tasknode.task_processing.constants import (
    TaskType, 
    INITIATION_RITE_XRP_COST, 
    TASK_PATTERNS,
    DISCORD_SUPER_USER_IDS
)
from tasknode.task_processing.user_context_parsing import UserTaskParser
from tasknode.task_processing.core_business_logic import TaskManagementRules
from tasknode.chatbots.personas.odv import odv_system_prompt
from tasknode.chatbots.odv_sprint_planner import ODVSprintPlannerO1
from tasknode.chatbots.odv_context_doc_improvement import ODVContextDocImprover
from tasknode.chatbots.corbanu_beta import CorbanuChatBot
from tasknode.chatbots.odv_focus_analyzer import ODVFocusAnalyzer
from tasknode.chatbots.discord_modals import (
    VerifyAddressModal,
    WalletInfoModal,
    SeedModal,
    PFTTransactionModal,
    XRPTransactionModal,
    AcceptanceModal,
    RefusalModal,
    InitiationModal,
    UpdateLinkModal,
    CompletionModal,
    VerificationModal
)

@dataclass
class AccountInfo:
    address: str
    username: str = ''
    xrp_balance: float = 0
    pft_balance: float = 0
    transaction_count: int = 0
    monthly_pft_avg: float = 0
    weekly_pft_avg: float = 0
    google_doc_link: Optional[str] = None

@dataclass
class DeathMarchSettings:
    # Configuration
    timezone: str
    start_time: time    # Daily start time
    end_time: time      # Daily end time
    check_interval: int # Minutes between check-ins
    # Session-specific data
    channel_id: Optional[int] = None
    session_start: Optional[datetime] = None
    session_end: Optional[datetime] = None
    last_checkin: Optional[datetime] = None

class TaskNodeDiscordBot(discord.Client):

    NON_EPHEMERAL_USERS = {402536023483088896, 471510026696261632}

    def __init__(
            self, 
            *args,
            openai_request_tool: OpenAIRequestTool,
            tasknode_utilities: TaskNodeUtilities,
            user_task_parser: UserTaskParser,
            nodetools: ServiceContainer,
            **kwargs
        ):
        super().__init__(*args, **kwargs)
        # Get network configuration and set network-specific attributes
        self.network_config = nodetools.network_config
        self.node_config = nodetools.node_config
        self.remembrancer = self.node_config.remembrancer_address

        # Initialize components
        self.openai_request_tool = openai_request_tool
        self.tasknode_utilities = tasknode_utilities
        self.user_task_parser = user_task_parser
        self.openrouter_tool = nodetools.dependencies.openrouter
        self.generic_pft_utilities = nodetools.dependencies.generic_pft_utilities
        self.transaction_repository = nodetools.dependencies.transaction_repository
        self.db_connection_manager = nodetools.db_connection_manager  # For Corbanu

        # Set network-specific attributes
        self.default_openai_model = global_constants.DEFAULT_OPEN_AI_MODEL
        self.conversations = {}
        self.user_seeds = {}
        self.doc_improvers = {}
        self.sprint_planners = {}  # Dictionary: user_id -> ODVSprintPlanner instance
        self.user_steps = {}       # Dictionary: user_id -> current step in the sprint process
        self.user_questions = {}
        self.user_deathmarch_settings: Dict[int, DeathMarchSettings] = {}
        self.death_march_tasks = {}
        
        self.tree = app_commands.CommandTree(self)

        # Caches for dataframes to enable deferred modals
        self.pending_tasks_cache = {}
        self.refuseable_tasks_cache = {}
        self.accepted_tasks_cache = {}
        self.verification_tasks_cache = {}
        self.cache_timeout = 300  # seconds

        self.notification_queue: asyncio.Queue = nodetools.notification_queue

    def is_special_user_non_ephemeral(self, interaction: discord.Interaction) -> bool:
        """Return False if the user is not in the NON_EPHEMERAL_USERS set, else True."""
        output = not (interaction.user.id in self.NON_EPHEMERAL_USERS)
        return output

    async def setup_hook(self):
        """Sets up the slash commands for the bot and initiates background tasks."""
        guild_id = self.node_config.discord_guild_id
        guild = Object(id=guild_id)
        
        self.bg_task = self.loop.create_task(
            self.transaction_notifier(),
            name="DiscordBotTransactionNotifier"
        )

        # # Prevents duplicate commands but also makes launch slow.
        # self.tree.clear_commands(guild=guild)
        # await self.tree.sync(guild=guild)
    
        @self.event
        async def on_guild_available(guild: discord.Guild):
            """Log when a guild becomes available."""
            logger.info(f"Guild {guild.name} (ID: {guild.id}) is available")

        @self.event
        async def on_member_remove(user: discord.User):
            """Handle member ban events by deauthorizing their addresses."""
            logger.info(f"Member remove event received for user {user.name} (ID: {user.id}). Deauthorizing addresses...")
            try:
                # Remove their seed if it exists
                self.user_seeds.pop(user.id, None)
                
                # Deauthorize all addresses associated with this Discord user
                await self.transaction_repository.deauthorize_addresses(
                    auth_source='discord',
                    auth_source_user_id=str(user.id)
                )
                
            except Exception as e:
                logger.error(
                    f"Error deauthorizing addresses for banned user {user.name} (ID: {user.id}): {e}"
                )
                logger.error(traceback.format_exc())

        # @self.event
        # async def on_socket_raw_receive(msg):
        #     """Debug log for raw events."""
        #     logger.debug(f"Raw Socket Event: {msg}")

        @self.tree.command(name="pf_verify", description="Verify an XRP address for use with Post-Fiat features")
        async def pf_verify(interaction: Interaction):
            # Create and send the verification modal
            modal = VerifyAddressModal(client=interaction.client)
            await interaction.response.send_modal(modal)

        @self.tree.command(name="pf_new_wallet", description="Generate a new XRP wallet")
        async def pf_new_wallet(interaction: Interaction):
            # Generate the wallet
            new_wallet = Wallet.create()

            # Create the modal with the client reference and send it
            modal = WalletInfoModal(
                classic_address=new_wallet.classic_address,
                wallet_seed=new_wallet.seed,
                client=interaction.client
            )
            await interaction.response.send_modal(modal)

        @self.tree.command(name="pf_show_seed", description="Show your stored seed")
        async def pf_show_seed(interaction: discord.Interaction):
            user_id = interaction.user.id
            
            # Check if the user has a stored seed
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                # Create and send an ephemeral message with the seed
                await interaction.response.send_message(
                    f"Your stored seed is: {seed}\n"
                    "This message will be deleted in 30 seconds for security reasons.",
                    ephemeral=True,
                    delete_after=30
                )
            else:
                await interaction.response.send_message(
                    "No seed found for your account. Use /pf_store_seed to store a seed first.",
                    ephemeral=True
                )

        @self.tree.command(name="pf_guide", description="Show a guide of all available commands")
        async def pf_guide(interaction: discord.Interaction):
            guide_text = f"""
# Post Fiat Discord Bot Guide

### Info Commands
1. /pf_guide: Show this guide
2. /pf_my_wallet: Show information about your stored wallet.
3. /wallet_info: Get information about a specific wallet address.
4. /pf_show_seed: Display your stored seed 
5. /pf_rewards: Show recent PFT rewards.
6. /pf_outstanding: Show your outstanding tasks.

### Initiation
1. /pf_new_wallet: Generate a new XRP wallet. You need to fund via Coinbase etc to continue
2. /pf_store_seed: Securely store your wallet seed.
3. /pf_initiate: Initiate your commitment to the Post Fiat system, get access to PFT and initial grant
4. /pf_update_link: Update your Google Doc link

### Task Request
1. /pf_request_task: Request a new Post Fiat task.
2. /pf_accept: View and accept available tasks.
3. /pf_refuse: View and refuse available tasks.
4. /pf_initial_verification: Submit a completed task for verification.
5. /pf_final_verification: Submit final verification for a task to receive reward

### Transaction
1. /xrp_send: Send XRP to a destination address with a memo.
2. /pf_send: Open a transaction form to send PFT tokens with a memo.
3. /pf_log: take notes re your workflows, with optional encryption

## Post Fiat operates on a Google Document.
1. Set your Document to be shared (File/Share/Share With Others/Anyone With Link)
2. The PF Initiate Function requires a document and a verbal committment
3. Place the following section in your document:
___x TASK VERIFICATION SECTION START x___ 
task verification details are here 
___x TASK VERIFICATION SECTION END x___

## Local Version
You can run a local version of the wallet. Please reference the Post Fiat Github
https://github.com/postfiatorg/pftpyclient

Note: XRP wallets need {global_constants.MIN_XRP_BALANCE} XRP to transact, and initiation rites cost {INITIATION_RITE_XRP_COST} XRP. 
So you need at least {global_constants.MIN_XRP_BALANCE + INITIATION_RITE_XRP_COST} XRP to start, 
but we recommend funding with a bit more to cover ongoing transaction fees.
"""
            
            await interaction.response.send_message(guide_text, ephemeral=True)

        @self.tree.command(name="pf_my_wallet", description="Show your wallet information")
        async def pf_my_wallet(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            
            # Defer the response to avoid timeout
            await interaction.response.defer(ephemeral=ephemeral_setting)
            
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "No seed found for your account. Use /pf_store_seed to store a seed first.",
                    ephemeral=True
                )
                return

            try:
                seed = self.user_seeds[user_id]
                logger.debug(f"TaskNodeDiscordBot.setup_hook.pf_my_wallet: Spawning wallet to fetch info for {interaction.user.name}")
                wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)
                wallet_address = wallet.classic_address

                # Get account info
                account_info = await self.generate_basic_balance_info_string(address=wallet.address)
                
                # Get recent messages
                incoming_messages, outgoing_messages = await self.generic_pft_utilities.get_recent_messages(wallet_address)

                # Split long strings if they exceed Discord's limit
                def truncate_field(content, max_length=1024):
                    if len(content) > max_length:
                        return content[:max_length-3] + "..."
                    return content

                # Create multiple embeds if needed
                embeds = []
                
                # First embed with basic info
                embed = discord.Embed(title="Your Wallet Information", color=0x00ff00)
                embed.add_field(name="Wallet Address", value=wallet_address, inline=False)
                
                # Split account info into multiple fields if needed
                if len(account_info) > 1024:
                    parts = [account_info[i:i+1024] for i in range(0, len(account_info), 1024)]
                    for i, part in enumerate(parts):
                        embed.add_field(name=f"Balance Information {i+1}", value=part, inline=False)
                else:
                    embed.add_field(name="Balance Information", value=account_info, inline=False)
                
                embeds.append(embed)

                if incoming_messages or outgoing_messages:
                    embed2 = discord.Embed(title="Recent Transactions", color=0x00ff00)
                    
                    if incoming_messages:
                        incoming = truncate_field(incoming_messages)
                        embed2.add_field(name="Most Recent Incoming Transaction", value=incoming, inline=False)
                    
                    if outgoing_messages:
                        outgoing = truncate_field(outgoing_messages)
                        embed2.add_field(name="Most Recent Outgoing Transaction", value=outgoing, inline=False)
                    
                    embeds.append(embed2)

                # Send all embeds
                await interaction.followup.send(embeds=embeds, ephemeral=ephemeral_setting)
            
            except Exception as e:
                error_message = f"An unexpected error occurred: {str(e)}. Please try again later or contact support if the issue persists."
                logger.error(f"TaskNodeDiscordBot.pf_my_wallet: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(error_message, ephemeral=True)

        @self.tree.command(name="wallet_info", description="Get information about a wallet")
        async def wallet_info(interaction: discord.Interaction, wallet_address: str):
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            try:
                account_info = await self.generate_basic_balance_info_string(address=wallet_address, owns_wallet=False)
                
                # Create an embed for better formatting
                embed = discord.Embed(title="Wallet Information", color=0x00ff00)
                embed.add_field(name="Wallet Address", value=wallet_address, inline=False)
                embed.add_field(name="Account Info", value=account_info, inline=False)
                
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral_setting)
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.wallet_info: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)

        @self.tree.command(name="admin_debug_full_user_context", description="Return the full user context")
        async def admin_debug_full_user_context(interaction: discord.Interaction, wallet_address: str):
            # Check if the user has permission (matches the specific ID)
            if interaction.user.id not in DISCORD_SUPER_USER_IDS:
                await interaction.response.send_message(
                    "You don't have permission to use this command.", 
                    ephemeral=True
                )
                return
            try:
                await interaction.response.defer(ephemeral=True)
                full_user_context = await self.user_task_parser.get_full_user_context_string(
                    account_address=wallet_address,
                    n_memos_in_context=20
                )
                await self.send_long_interaction_response(
                        interaction, 
                        f"\n{full_user_context}", 
                        ephemeral=True
                    )
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.admin_debug_full_user_context: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while fetching the full user context: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="admin_change_ephemeral_setting", description="Change the ephemeral setting for self")
        async def admin_change_ephemeral_setting(interaction: discord.Interaction, public: bool):
            # Check if the user has permission (matches the specific ID)
            if interaction.user.id not in DISCORD_SUPER_USER_IDS:
                await interaction.response.send_message(
                    "You don't have permission to use this command.", 
                    ephemeral=True
                )
                return
            
            user_id = interaction.user.id
            if public:
                self.NON_EPHEMERAL_USERS.add(user_id)
                setting = "PUBLIC"
            else:
                self.NON_EPHEMERAL_USERS.discard(user_id)
                setting = "PRIVATE"
            
            await interaction.response.send_message(
                f"Your messages will now be {setting}",
                ephemeral=True
            )

        @self.tree.command(name="pf_send", description="Open a transaction form")
        async def pf_send(interaction: Interaction):
            user_id = interaction.user.id

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.response.send_message(
                    "You must store a seed using /store_seed before initiating a transaction.", 
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Pass the user's wallet to the modal
            await interaction.response.send_modal(
                PFTTransactionModal(
                    wallet=wallet,
                    generic_pft_utilities=self.generic_pft_utilities
                )
            )

        @self.tree.command(name="xrp_send", description="Send XRP to a destination address")
        async def xrp_send(interaction: discord.Interaction):
            user_id = interaction.user.id
            
            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.response.send_message(
                    "You must store a seed using /pf_store_seed before initiating a transaction.", ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Pass the user's wallet to the modal
            await interaction.response.send_modal(
                XRPTransactionModal(
                    wallet=wallet,
                    generic_pft_utilities=self.generic_pft_utilities
                )
            )

        @self.tree.command(name="pf_store_seed", description="Store a seed")
        async def store_seed(interaction: discord.Interaction):
            await interaction.response.send_modal(SeedModal(client=self))
            logger.debug(f"TaskNodeDiscordBot.store_seed: Seed storage command executed by {interaction.user.name}")

        @self.tree.command(name="pf_initiate", description="Initiate your commitment")
        async def pf_initiate(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /store_seed before initiating.", 
                    ephemeral=ephemeral_setting
                )
                return

            try:
                # Spawn the user's wallet
                logger.debug(f"TaskNodeDiscordBot.pf_initiate: Spawning wallet to initiate for {interaction.user.name}")
                username = interaction.user.name
                seed = self.user_seeds[user_id]
                wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)

                if not (RuntimeConfig.USE_TESTNET and RuntimeConfig.ENABLE_REINITIATIONS):
                    if await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                        logger.debug(f"Blocking re-initiation for user {interaction.user.name}, wallet address {wallet.address}")
                        await interaction.followup.send(
                            "You've already completed an initiation rite. Re-initiation is not allowed.", 
                            ephemeral=ephemeral_setting
                        )
                        return

                # Create a button to trigger the modal
                async def button_callback(button_interaction: discord.Interaction):
                    await button_interaction.response.send_modal(
                        InitiationModal(
                            seed=seed,
                            username=username,
                            client_instance=self,
                            tasknode_utilities=self.tasknode_utilities,
                            ephemeral_setting=ephemeral_setting
                        )
                    )

                button = Button(label="Begin Initiation", style=discord.ButtonStyle.primary)
                button.callback = button_callback

                view = View()
                view.add_item(button)
                await interaction.followup.send(
                    "Click the button below to begin your initiation:", 
                    view=view, 
                    ephemeral=ephemeral_setting
                )

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_initiate: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred during initiation: {str(e)}", ephemeral=ephemeral_setting)

        @self.tree.command(name="pf_update_link", description="Update your Google Doc link")
        async def pf_update_link(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed first.",
                    ephemeral=ephemeral_setting
                )
                return
            
            try:
                logger.debug(f"TaskNodeDiscordBot.pf_update_link: Spawning wallet for {interaction.user.name} to update google doc link")
                seed = self.user_seeds[user_id]
                username = interaction.user.name
                wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)

                # Check initiation status
                if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                    await interaction.followup.send(
                        "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                        ephemeral=ephemeral_setting
                    )
                    return

                # Create a button to trigger the modal
                async def button_callback(button_interaction: discord.Interaction):
                    await button_interaction.response.send_modal(
                        UpdateLinkModal(
                            seed=seed,
                            username=username,
                            client_instance=self,
                            tasknode_utilities=self.tasknode_utilities,
                            ephemeral_setting=ephemeral_setting
                        )
                    )

                button = Button(label="Update Google Doc Link", style=discord.ButtonStyle.primary)
                button.callback = button_callback

                view = View()
                view.add_item(button)
                await interaction.followup.send(
                    "Click the button below to update your Google Doc link:", 
                    view=view, 
                    ephemeral=ephemeral_setting
                )

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_update_link: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred during update: {str(e)}", ephemeral=True)

        @self.tree.command(name="odv_sprint", description="Start an ODV sprint planning session")
        async def odv_sprint(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before starting an ODV sprint planning session.", 
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return

            try:
                odv_planner = await ODVSprintPlannerO1.create(
                    account_address=wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities
                )
                self.sprint_planners[user_id] = odv_planner
                logger.debug(f"TaskNodeDiscordBot.odv_sprint: Initialized ODV sprint planner for {interaction.user.name}")

                # Potentially long operation
                logger.debug(f"TaskNodeDiscordBot.odv_sprint: Getting initial response for {interaction.user.name}")
                initial_response = await odv_planner.get_response_async("Please provide your context analysis.")

                # Use the helper function to send the possibly long response
                logger.debug(f"TaskNodeDiscordBot.odv_sprint: Sending initial response for {interaction.user.name}")
                await self.send_long_interaction_response(
                    interaction, 
                    f"**ODV Sprint Planning Initialized**\n\n{initial_response}", 
                    ephemeral=ephemeral_setting
                )
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.odv_sprint: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while initializing the ODV sprint planning session: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="odv_sprint_reply", description="Continue the ODV sprint planning session")
        @app_commands.describe(message="Your next input to ODV")
        async def odv_sprint_reply(interaction: discord.Interaction, message: str):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)

            if user_id not in self.sprint_planners:
                await interaction.response.send_message(
                    "No active ODV sprint planning session. Start one with /odv_sprint.", 
                    ephemeral=ephemeral_setting
                )
                return

            odv_planner: ODVSprintPlannerO1 = self.sprint_planners[user_id]
            logger.debug(f"TaskNodeDiscordBot.odv_sprint_reply: Continuing ODV sprint planning session for {interaction.user.name}")
            await interaction.response.defer(ephemeral=ephemeral_setting)

            try:
                # Now using async version
                logger.debug(f"TaskNodeDiscordBot.odv_sprint_reply: Getting response for {interaction.user.name}")
                response = await odv_planner.get_response_async(message)
                logger.debug(f"TaskNodeDiscordBot.odv_sprint_reply: Response received for {interaction.user.name}")
                await self.send_long_interaction_response(interaction, response, ephemeral=ephemeral_setting)
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.odv_sprint_reply: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="pf_configure_deathmarch", description="Configure your death march")
        async def pf_configure_deathmarch(interaction: discord.Interaction):
            # Common timezone options
            timezone_options = [
                SelectOption(label="US/Pacific", description="Los Angeles, Seattle, Vancouver (UTC-7/8)"),
                SelectOption(label="US/Mountain", description="Denver, Phoenix (UTC-6/7)"),
                SelectOption(label="US/Central", description="Chicago, Mexico City (UTC-5/6)"),
                SelectOption(label="US/Eastern", description="New York, Toronto, Miami (UTC-4/5)"),
                SelectOption(label="Europe/London", description="London, Dublin, Lisbon (UTC+0/1)"),
                SelectOption(label="Europe/Paris", description="Paris, Berlin, Rome (UTC+1/2)"),
                SelectOption(label="Asia/Tokyo", description="Tokyo, Seoul (UTC+9)"),
                SelectOption(label="Australia/Sydney", description="Sydney, Melbourne (UTC+10/11)"),
                SelectOption(label="Pacific/Auckland", description="Auckland, Wellington (UTC+12/13)")
            ]
            # Time options vary based on environment
            if RuntimeConfig.USE_TESTNET:
                # Testing: Allow any hour
                start_time_options = [
                    SelectOption(label=f"{hour:02d}:00", value=f"{hour:02d}:00") 
                    for hour in range(0, 24)  # 0-23 hours
                ]
                end_time_options = [
                    SelectOption(label=f"{hour:02d}:00", value=f"{hour:02d}:00") 
                    for hour in range(0, 24)  # 0-23 hours
                ]
                # Add shorter intervals for testing
                interval_options = [
                    SelectOption(label="1 minute", value="1", description="⚠️ Testing only"),
                    SelectOption(label="5 minutes", value="5", description="⚠️ Testing only"),
                    SelectOption(label="15 minutes", value="15", description="⚠️ Testing only"),
                    SelectOption(label="30 minutes", value="30"),
                    SelectOption(label="1 hour", value="60"),
                    SelectOption(label="2 hours", value="120")
                ]
            else:
                # Production: Restricted hours
                start_time_options = [
                    SelectOption(label=f"{hour:02d}:00", value=f"{hour:02d}:00") 
                    for hour in range(5, 13)  # 5 AM to 12 PM
                ]
                end_time_options = [
                    SelectOption(label=f"{hour:02d}:00", value=f"{hour:02d}:00") 
                    for hour in range(16, 24)  # 4 PM to 11 PM
                ]
                # Production intervals
                interval_options = [
                    SelectOption(label="30 minutes", value="30"),
                    SelectOption(label="1 hour", value="60"),
                    SelectOption(label="2 hours", value="120"),
                    SelectOption(label="3 hours", value="180"),
                    SelectOption(label="4 hours", value="240")
                ]
            user_id = interaction.user.id
            # 1. Check user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.response.send_message(
                    "You must store a seed using /pf_store_seed first.", 
                    ephemeral=True
                )
                return
            # Create the Select menus
            timezone_select = Select(
                custom_id="timezone",
                placeholder="Choose your timezone",
                options=timezone_options,
                row=0
            )
            
            start_time_select = Select(
                custom_id="start_time",
                placeholder="Choose start time",
                options=start_time_options,
                row=1
            )
            
            end_time_select = Select(
                custom_id="end_time",
                placeholder="Choose end time",
                options=end_time_options,
                row=2
            )
            
            interval_select = Select(
                custom_id="interval",
                placeholder="Choose check-in interval",
                options=interval_options,
                row=3
            )
            user_choices = {}
            
            async def select_callback(interaction: discord.Interaction):
                select_id = interaction.data["custom_id"]
                selected_value = interaction.data["values"][0]
                user_choices[select_id] = selected_value
                
                # Check if all selections have been made
                if len(user_choices) == 4:  # All selections made
                    try:
                        # Convert time strings to time objects
                        start_time = datetime.strptime(user_choices["start_time"], "%H:%M").time()
                        end_time = datetime.strptime(user_choices["end_time"], "%H:%M").time()
                        
                        # Create or update DeathMarchSettings
                        settings = DeathMarchSettings(
                            timezone=user_choices["timezone"],
                            start_time=start_time,
                            end_time=end_time,
                            check_interval=int(user_choices["interval"])
                        )
                        # Calculate costs
                        checks_per_day, daily_cost = self._calculate_death_march_costs(settings)
                        
                        # Store settings
                        self.user_deathmarch_settings[interaction.user.id] = settings
                        
                        settings_msg = (
                            f"Settings saved:\n"
                            f"• Timezone: {settings.timezone}\n"
                            f"• Focus window: {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}\n"
                            f"• Check-in interval: {settings.check_interval} minutes\n\n"
                            f"📊 Cost Analysis:\n"
                            f"• Check-ins per day: {checks_per_day}\n"
                            f"• Daily cost: {daily_cost} PFT\n"
                            f"• Weekly cost: {daily_cost * 7} PFT\n"
                            f"• Monthly cost: {daily_cost * 30} PFT\n\n"
                            "Use /pf_death_march_start to begin your death march."
                        )
                        
                        ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
                        await interaction.response.send_message(
                            settings_msg,
                            ephemeral=ephemeral_setting
                        )
                    except Exception as e:
                        await interaction.response.send_message(
                            f"An error occurred: {str(e)}",
                            ephemeral=True
                        )
                else:
                    await interaction.response.defer()
            # Attach callbacks
            timezone_select.callback = select_callback
            start_time_select.callback = select_callback
            end_time_select.callback = select_callback
            interval_select.callback = select_callback
            # Create view and add all selects
            view = discord.ui.View()
            view.add_item(timezone_select)
            view.add_item(start_time_select)
            view.add_item(end_time_select)
            view.add_item(interval_select)
            # Send the message with all dropdowns
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.send_message(
                "Please set your preferences:",
                view=view,
                ephemeral=ephemeral_setting
            )

        @self.tree.command(name="pf_death_march_start", description="Kick off a death march.")
        @app_commands.describe(days="Number of days to continue the death march")
        async def pf_death_march_start(interaction: discord.Interaction, days: int):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # 1. Check user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed first.", 
                    ephemeral=True
                )
                return
            seed = self.user_seeds[user_id]
            user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
            user_address = user_wallet.classic_address

            # 2. Check initiation
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=user_address):
                await interaction.followup.send(
                    "You must perform the initiation rite first ( /pf_initiate ).", 
                    ephemeral=True
                )
                return
            
            # 3. Check user has configured death march settings
            if user_id not in self.user_deathmarch_settings:
                await interaction.followup.send(
                    "You must set your death march configuration using /pf_configure_deathmarch first.", 
                    ephemeral=True
                )
                return
            
            # 4. Check if user is already in a death march
            if user_id in self.user_deathmarch_settings and self.user_deathmarch_settings[user_id].session_end is not None:
                await interaction.followup.send(
                    "You are already in an active death march. Use /pf_death_march_end to end it first.", 
                    ephemeral=True
                )
                return
            
            # Calculate cost based on check-in frequency
            settings = self.user_deathmarch_settings[user_id]
            checks_per_day, cost = self._calculate_death_march_costs(settings, days)
            # 5. Check user PFT balance
            try:
                user_pft_balance = self.generic_pft_utilities.get_pft_balance(user_address)
            except:
                await interaction.followup.send("Error fetching your PFT balance. Try again later.", ephemeral=True)
                return
            
            if user_pft_balance < cost:
                await interaction.followup.send(
                    f"You need {cost} PFT but only have {user_pft_balance} PFT.\n"
                    f"This cost is based on {checks_per_day} check-ins per day for {days} days.\n"
                    "Please acquire more PFT first.", 
                    ephemeral=ephemeral_setting
                )
                return
            # 6. Process payment
            memo_data = f"DEATH_MARCH Payment: {days} days, {checks_per_day} checks/day"
            
            try:
                response = await self.generic_pft_utilities.send_memo(
                    wallet_seed_or_wallet=user_wallet,
                    destination=self.node_config.remembrancer_address,  # Or wherever you want the PFT to go
                    memo=memo_data,
                    username=interaction.user.name,
                    chunk=False,
                    compress=False,
                    encrypt=False,
                    pft_amount=Decimal(cost)
                )
                if not self.generic_pft_utilities.verify_transaction_response(response):
                    raise Exception(f"Failed to send Death March payment: {response.result}")

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_death_march_start: Error sending memo: {e}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
                return
            
            # 7. Update death march settings
            session_start = datetime.now(timezone.utc)
            session_end = session_start + timedelta(days=days)
            
            settings.channel_id = interaction.channel_id
            settings.session_start = session_start
            settings.session_end = session_end
            settings.last_checkin = None
            # Create a new task for this user's death march
            task = self.loop.create_task(
                self.death_march_checker_for_user(user_id),
                name=f"death_march_{user_id}"
            )
            self.death_march_tasks[user_id] = task
            await interaction.followup.send(
                f"Death March started for {days} day(s).\n"
                f"• Cost: {cost} PFT ({checks_per_day} check-ins per day)\n"
                f"• Check-in window: {settings.start_time.strftime('%H:%M')} - {settings.end_time.strftime('%H:%M')} "
                f"({settings.timezone})\n"
                f"• Check-in interval: Every {settings.check_interval} minutes\n"
                f"• Session ends: {session_end} UTC\n\n"
                "Use /pf_death_march_end to stop it sooner (no refunds).",
                ephemeral=ephemeral_setting
            )

        @self.tree.command(name="pf_death_march_end", description="End your Death March session early (no refunds).")
        async def pf_death_march_end(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            # Check if user has settings and an active session
            if (user_id in self.user_deathmarch_settings and 
                    self.user_deathmarch_settings[user_id].session_end is not None):
                # Cancel the death march task
                if user_id in self.death_march_tasks:
                    self.death_march_tasks[user_id].cancel()
                    del self.death_march_tasks[user_id]
                settings = self.user_deathmarch_settings[user_id]
                # Clear session data but keep configuration
                settings.session_start = None
                settings.session_end = None
                settings.channel_id = None
                settings.last_checkin = None
                
                await interaction.response.send_message(
                    "Your Death March session has ended. Configuration saved for future use.",
                    ephemeral=ephemeral_setting
                )
            else:
                await interaction.response.send_message(
                    "You do not currently have a Death March session active.",
                    ephemeral=ephemeral_setting
                )

        @self.tree.command(name="odv_context_doc", description="Start an ODV context document improvement session")
        async def odv_context_doc(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before starting an ODV context document improvement session.", 
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return

            try:
                # Initialize the ODVContextDocImprover
                doc_improver = await ODVContextDocImprover.create(
                    account_address=wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities
                )

                # Ensure you have a dictionary to store doc improvers per user
                if not hasattr(self, 'doc_improvers'):
                    self.doc_improvers = {}

                self.doc_improvers[user_id] = doc_improver
                logger.debug(f"TaskNodeDiscordBot.odv_context_doc: Initialized ODV context document improver for {interaction.user.name}")

                # Potentially long operation: getting the initial suggestion
                logger.debug(f"TaskNodeDiscordBot.odv_context_doc: Getting initial response for {interaction.user.name}")
                initial_response = await doc_improver.get_response_async("Please provide your first improvement suggestion.")
                logger.debug(f"TaskNodeDiscordBot.odv_context_doc: Sending initial response for {interaction.user.name}")

                # Use the helper function to send the possibly long response
                await self.send_long_interaction_response(
                    interaction, 
                    f"**ODV Context Document Improvement Initialized**\n\n{initial_response}", 
                    ephemeral=ephemeral_setting
                )
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.odv_context_doc: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while initializing the ODV context document improvement session: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="odv_context_doc_reply", description="Continue the ODV context document improvement session")
        @app_commands.describe(message="Your next input to ODV")
        async def odv_context_doc_reply(interaction: discord.Interaction, message: str):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)

            # Check if we have a doc improver session in progress
            if not hasattr(self, 'doc_improvers') or user_id not in self.doc_improvers:
                await interaction.response.send_message(
                    "No active ODV context document improvement session. Start one with /odv_context_doc.", 
                    ephemeral=True
                )
                return

            doc_improver: ODVContextDocImprover = self.doc_improvers[user_id]
            logger.debug(f"TaskNodeDiscordBot.odv_context_doc_reply: Continuing ODV context document improvement session for {interaction.user.name}")
            await interaction.response.defer(ephemeral=ephemeral_setting)

            try:
                logger.debug(f"TaskNodeDiscordBot.odv_context_doc_reply: Getting response for {interaction.user.name}")
                response = await doc_improver.get_response_async(message)
                logger.debug(f"TaskNodeDiscordBot.odv_context_doc_reply: Response received for {interaction.user.name}")
                await self.send_long_interaction_response(interaction, response, ephemeral=ephemeral_setting)
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.odv_context_doc_reply: An error occurred: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="corbanu_offering", description="Generate a Corbanu offering")
        async def corbanu_offering(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before using this command.",
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.classic_address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return
            
            # Return the existing question if the user has one
            if user_id in self.user_questions:
                await interaction.followup.send(
                    f"Corbanu Offering:\n\n{self.user_questions[user_id]}",
                    ephemeral=ephemeral_setting
                )
                return

            try:
                # Initialize the CorbanuChatBot instance
                logger.debug(f"TaskNodeDiscordBot.corbanu_offering: {interaction.user.name} has requested a Corbanu offering. Initializing CorbanuChatBot instance.")
                corbanu = await CorbanuChatBot.create(
                    account_address=wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities,
                    tasknode_utilities=self.tasknode_utilities,
                    db_connection_manager=self.db_connection_manager
                )

                # Generate a question as the Corbanu offering 
                question = await corbanu.generate_question()
                logger.debug(f"TaskNodeDiscordBot.corbanu_offering: Question generated for {interaction.user.name}: {question}")

                # Store the question so we can use it in /corbanu_reply
                self.user_questions[user_id] = question

                await interaction.followup.send(
                    f"Corbanu Offering:\n\n{question}",
                    ephemeral=ephemeral_setting
                )

            except Exception as e:
                logger.error(f"Error in corbanu_offering: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while generating Corbanu offering: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="corbanu_reply", description="Reply to the last Corbanu offering")
        @app_commands.describe(answer="Your answer to the last Corbanu question")
        async def corbanu_reply(interaction: discord.Interaction, answer: str):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before using this command.",
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.classic_address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.",
                    ephemeral=True
                )
                return

            if user_id not in self.user_questions:
                await interaction.followup.send(
                    "No Corbanu question found. Please use /corbanu_offering first.",
                    ephemeral=ephemeral_setting
                )
                return

            try:
                question = self.user_questions[user_id]
                logger.debug(f"TaskNodeDiscordBot.corbanu_reply: Received user answer for {interaction.user.name}.\nQuestion:\n{question}\nAnswer: \n{answer}")
                
                corbanu = await CorbanuChatBot.create(
                    account_address=wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities,
                    tasknode_utilities=self.tasknode_utilities,
                    db_connection_manager=self.db_connection_manager
                )

                scoring = await corbanu.generate_user_question_scoring_output(
                    original_question=question,
                    user_answer=answer,
                    account_address=wallet.classic_address
                )

                reward_value = scoring.get('reward_value', 0)
                reward_description = scoring.get('reward_description', 'No description')

                full_message = ("**Corbanu Summary**\n"
                                "CORBANU_OFFERING\n"
                                f"Q: {question}\n\n"
                                f"A: {answer}\n\n"
                                f"Reward: {reward_value} PFT\n"
                                f"{reward_description}")
                
                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=full_message,
                    ephemeral=ephemeral_setting
                )

                # Now the user will send the Q&A to the remembrancer
                # Similar to pf_log, we need to ensure a handshake if encrypt=True
                encrypt = True  # We assume we always encrypt the Q&A to the remembrancer.
                user_name = interaction.user.name
                message_obj = await interaction.followup.send(
                    "Preparing to send Q&A to the remembrancer...",
                    ephemeral=ephemeral_setting,
                    wait=True
                )

                handshake_success, user_key, counterparty_key, message_obj = await self._ensure_handshake(
                    interaction=interaction,
                    seed=seed,
                    counterparty=self.remembrancer,
                    username=user_name,
                    command_name="corbanu_reply",
                    message_obj=message_obj
                )
                if not handshake_success:
                    logger.error(f"TaskNodeDiscordBot.corbanu_reply: Handshake failed for {interaction.user.name}.")
                    await message_obj.edit(content="Handshake failed. Aborting operation.")
                    return
                
                await message_obj.edit(content="Handshake verified. Proceeding to send memo...")

                # No PFT here, just sending the message
                # NOTE: This means we won't see this memo if we filter by PFT
                pft_amount=Decimal(0)

                # Send Q&A from user wallet to remembrancer
                try:
                    response = await self.generic_pft_utilities.send_memo(
                        wallet_seed_or_wallet=wallet,
                        username="Corbanu",  # This is memo_format
                        destination=self.remembrancer,
                        memo=full_message,
                        chunk=True,
                        compress=True,
                        encrypt=encrypt,
                        pft_amount=pft_amount,
                        disable_pft_check=True
                    )

                    if not self.generic_pft_utilities.verify_transaction_response(response):
                        raise Exception(f"Failed to send Q&A message to remembrancer: {response.result}")

                except Exception as e:
                    logger.error(f"TaskNodeDiscordBot.corbanu_reply: Error sending memo: {e}")
                    logger.error(traceback.format_exc())
                    await message_obj.edit(content=f"An error occurred while sending the Q&A message: {str(e)}")
                    return

                last_response = response[-1] if isinstance(response, list) else response
                if pft_amount > 0:
                    transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object(
                        response=last_response
                    )
                else:
                    transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object__standard_xrp(
                        response=last_response
                    )
                clean_string = transaction_info['clean_string']

                await message_obj.edit(
                    content=f"Q&A message sent to remembrancer successfully. Last chunk details:\n{clean_string}"
                )

                # Now send the reward from the node to the user
                foundation_seed = self.generic_pft_utilities.credential_manager.get_credential(
                    f"{self.node_config.node_name}__v1xrpsecret"
                )
                foundation_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(foundation_seed)

                short_reward_message = "Corbanu Reward"

                # Check daily reward limit
                remaining_daily_limit = await corbanu.check_daily_reward_limit(account_address=wallet.classic_address)
                reward_value = min(reward_value, remaining_daily_limit)

                # Check per-offering reward limit
                reward_value = min(reward_value, corbanu.MAX_PER_OFFERING_REWARD_VALUE)

                logger.debug(f"TaskNodeDiscordBot.corbanu_reply: Sending reward of {reward_value} PFT to {wallet.classic_address}")
                
                try:
                    reward_tx = await self.generic_pft_utilities.send_memo(
                        wallet_seed_or_wallet=foundation_wallet,
                        destination=wallet.classic_address,
                        memo=short_reward_message,
                        username="Corbanu",
                        chunk=False,
                        compress=False,
                        encrypt=False,
                        pft_amount=Decimal(reward_value)
                    )

                    if not self.generic_pft_utilities.verify_transaction_response(reward_tx):
                        raise Exception(f"Failed to send reward transaction: {reward_tx.result}")

                except Exception as e:
                    logger.error(f"TaskNodeDiscordBot.corbanu_reply: Error sending reward memo: {e}")
                    logger.error(traceback.format_exc())
                    await message_obj.edit(content=f"An error occurred while sending the reward: {str(e)}")
                    return

                # Confirm reward sent
                reward_info = self.generic_pft_utilities.extract_transaction_info_from_response_object(
                    reward_tx
                )
                reward_clean_string = "Reward transaction sent successfully:\n" + reward_info['clean_string']

                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=reward_clean_string,
                    ephemeral=ephemeral_setting
                )

                # Clear stored question
                del self.user_questions[user_id]

            except Exception as e:
                logger.error(f"Error in corbanu_reply: {str(e)}")
                logger.error(traceback.format_exc())
                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=f"An error occurred while processing your reply: {str(e)}",
                    ephemeral=True
                )

        @self.tree.command(name="corbanu_request", description="Send a request to Corbanu and get a response from Angron or Fulgrim")
        @app_commands.describe(message="Your message to Corbanu")
        async def corbanu_request(interaction: discord.Interaction, message: str):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before using this command.",
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.classic_address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return

            try:
                # Add the user message to their conversation history
                if user_id not in self.conversations:
                    self.conversations[user_id] = []

                self.conversations[user_id].append({"role": "user", "content": message})

                # Create CorbanuChatBot instance
                corbanu = await CorbanuChatBot.create(
                    account_address=wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities,
                    tasknode_utilities=self.tasknode_utilities,
                    db_connection_manager=self.db_connection_manager
                )

                # Get Corbanu's response asynchronously
                response = await corbanu.get_response_async(message)

                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=response,
                    ephemeral=ephemeral_setting
                )

                # Append Corbanu's response to conversation history
                self.conversations[user_id].append({"role": "assistant", "content": response})

                # Combine USER MESSAGE + CORBANU RESPONSE
                combined_message = f"USER MESSAGE:\n{message}\n\nCORBANU RESPONSE:\n{response}"

                # Summarize the combined message before sending to remembrancer
                summarized_message = await corbanu.summarize_text(combined_message, max_length=900)

                encrypt = True
                user_name = interaction.user.name

                # Notify user we're sending to remembrancer
                message_obj = await interaction.followup.send(
                    "Sending the Q&A record (summarized) to the remembrancer...",
                    ephemeral=ephemeral_setting,
                    wait=True
                )

                # Ensure handshake
                if encrypt:
                    handshake_success, user_key, counterparty_key, message_obj = await self._ensure_handshake(
                        interaction=interaction,
                        seed=seed,
                        counterparty=self.remembrancer,
                        username=user_name,
                        command_name="corbanu_request",
                        message_obj=message_obj
                    )
                    if not handshake_success:
                        return
                    
                    await message_obj.edit(content="Handshake verified. Proceeding to send memo...")

                # No PFT here, just sending the message
                # NOTE: This means we won't see this memo if we filter by PFT
                pft_amount = Decimal(0)

                # Send summarized message from user's wallet to remembrancer
                try:
                    send_response = await self.generic_pft_utilities.send_memo(
                        wallet_seed_or_wallet=wallet,
                        username=user_name,
                        destination=self.remembrancer,
                        memo=summarized_message,
                        chunk=True,
                        compress=True,
                        encrypt=encrypt,
                        pft_amount=pft_amount,
                        disable_pft_check=True
                    )
                    
                    if not self.generic_pft_utilities.verify_transaction_response(send_response):
                        raise Exception(f"Failed to send summarized Q&A record: {send_response.result}")
        
                except Exception as e:
                    logger.error(f"TaskNodeDiscordBot.corbanu_request: Error sending memo: {e}")
                    logger.error(traceback.format_exc())
                    await message_obj.edit(content=f"An error occurred while sending the summarized Q&A record: {str(e)}")
                    return

                last_response = send_response[-1] if isinstance(send_response, list) else send_response
                # TODO: Refactor these two methods into a single method
                if pft_amount > 0:
                    transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object(last_response)
                else:
                    transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object__standard_xrp(last_response)
                clean_string = transaction_info['clean_string']

                await message_obj.edit(
                    content=f"Summarized Q&A record sent to remembrancer successfully. Last chunk details:\n{clean_string}"
                )

            except Exception as e:
                logger.error(f"Error in corbanu_request: {str(e)}")
                logger.error(traceback.format_exc())
                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=f"An error occurred: {str(e)}",
                    ephemeral=True
                )

        @self.tree.command(name="pf_outstanding", description="Show your outstanding tasks and verification tasks")
        async def pf_outstanding(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)
            
            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before viewing outstanding tasks.", 
                    ephemeral=True
                )
                return

            seed = self.user_seeds[user_id]
            logger.debug(f"TaskNodeDiscordBot.setup_hook.pf_outstanding: Spawning wallet to fetch tasks for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return

            try:
                # Get the unformatted output message
                output_message = await self.create_full_outstanding_pft_string(account_address=wallet.address)
                
                # Format the message using the formatting function
                formatted_chunks = self.format_tasks_for_discord(output_message)
                
                # Send the first chunk
                await interaction.followup.send(formatted_chunks[0], ephemeral=ephemeral_setting)

                # Send the rest of the chunks
                for chunk in formatted_chunks[1:]:
                    await interaction.followup.send(chunk, ephemeral=ephemeral_setting)

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_outstanding: Error fetching outstanding tasks: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while fetching your outstanding tasks: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(name="pf_request_task", description="Request a Post Fiat task")
        async def pf_task_slash(interaction: discord.Interaction, task_request: str):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has stored a seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before generating a task.", 
                    ephemeral=True
                )
                return

            # Get the user's seed and other necessary information
            seed = self.user_seeds[user_id]
            user_name = interaction.user.name
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)
            
            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=True
                )
                return
            
            try:
                # Send the Post Fiat request
                response = await self.tasknode_utilities.discord__send_postfiat_request(
                    user_request=task_request,
                    user_name=user_name,
                    user_wallet=wallet
                )
                
                # Extract transaction information
                transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object(response=response)
                clean_string = transaction_info['clean_string']
                
                # Send the response
                await interaction.followup.send(f"Task Requested with Details: {clean_string}", ephemeral=ephemeral_setting)
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_task_slash: Error during task request: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred while processing your request: {str(e)}", ephemeral=True)

        @self.tree.command(name="pf_accept", description="Accept tasks")
        async def pf_accept_menu(interaction: discord.Interaction):
            # Fetch the user's seed
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            if user_id not in self.user_seeds:
                await interaction.followup.send("You must store a seed using /store_seed before accepting tasks.", ephemeral=ephemeral_setting)
                return

            seed = self.user_seeds[user_id]

            logger.debug(f"TaskNodeDiscordBot.pf_accept_menu: Spawning wallet to fetch tasks for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            # Fetch proposal acceptance pairs
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=wallet.address)

            # Get pending proposals
            pending_tasks = await self.user_task_parser.get_pending_proposals(account=memo_history)

            # Return if proposal acceptance pairs are empty
            if pending_tasks.empty:
                await interaction.followup.send("You have no tasks to accept.", ephemeral=ephemeral_setting)
                return
            
            # Cache the pending tasks
            self.pending_tasks_cache[user_id] = pending_tasks

            # Create dropdown options based on the non-accepted tasks
            options = [
                SelectOption(
                    label=task_id, 
                    description=str(pending_tasks.loc[task_id, 'proposal'])[:100],  # get just the proposal text
                    value=task_id
                )
                for task_id in pending_tasks.index
            ]

            # Create the Select menu
            select = Select(placeholder="Choose a task to accept", options=options)

            # Create the Select menu with its callback
            async def select_callback(interaction: discord.Interaction):
                selected_task_id = select.values[0]
                cached_tasks = self.pending_tasks_cache[user_id]
                task_text = str(cached_tasks.loc[selected_task_id, 'proposal'])
                self.pending_tasks_cache.pop(user_id, None)

                await interaction.response.send_modal(
                    AcceptanceModal(
                        task_id=selected_task_id,
                        task_text=task_text,
                        seed=seed,
                        user_name=interaction.user.name,
                        tasknode_utilities=self.tasknode_utilities,
                        ephemeral_setting=ephemeral_setting
                    )
                )

            async def on_timeout():
                # Clear cache
                self.pending_tasks_cache.pop(user_id, None)
                # Update message to indicate expiration
                try:
                    await interaction.edit_original_response(
                        content="This task selection menu has expired. Please run /pf_accept again.",
                        view=None  # This removes the select menu
                    )
                except Exception as e:
                    logger.error(f"Failed to update message on timeout: {e}")

            # Attach the callback to the select element
            select.callback = select_callback

            # Create a view and add the select element to it
            view = View(timeout=self.cache_timeout)
            view.add_item(select)
            view.on_timeout = on_timeout

            # Send the message with the dropdown menu
            await interaction.followup.send("Please choose a task to accept:", view=view, ephemeral=ephemeral_setting)

        @self.tree.command(name="pf_refuse", description="Refuse tasks")
        async def pf_refuse_menu(interaction: discord.Interaction):
            # Fetch the user's seed
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /store_seed before refusing tasks.", 
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]

            logger.debug(f"TaskNodeDiscordBot.pf_refuse_menu: Spawning wallet to fetch tasks for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return
            
            # Fetch account history
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=wallet.address)

            # Get refuseable proposals
            refuseable_tasks = await self.user_task_parser.get_refuseable_proposals(account=memo_history)

            # Return if proposal refusal pairs are empty
            if refuseable_tasks.empty:
                await interaction.followup.send("You have no tasks to refuse.", ephemeral=ephemeral_setting)
                return

            # Cache the refuseable tasks
            self.refuseable_tasks_cache[user_id] = refuseable_tasks

            # Create dropdown options based on the non-accepted tasks
            options = [
                SelectOption(
                    label=task_id, 
                    description=str(refuseable_tasks.loc[task_id, 'proposal'])[:100], 
                    value=task_id
                )
                for task_id in refuseable_tasks.index
            ]

            # Create the Select menu
            select = Select(placeholder="Choose a task to refuse", options=options)

            # Define the callback for when a user selects an option
            async def select_callback(interaction: discord.Interaction):
                selected_task_id = select.values[0]
                cached_tasks = self.refuseable_tasks_cache[user_id]
                task_text = str(cached_tasks.loc[selected_task_id, 'proposal'])
                self.refuseable_tasks_cache.pop(user_id, None)
    
                await interaction.response.send_modal(
                    RefusalModal(
                        task_id=selected_task_id,
                        task_text=task_text,
                        seed=seed,
                        user_name=interaction.user.name,
                        tasknode_utilities=self.tasknode_utilities,
                        ephemeral_setting=ephemeral_setting
                    )
                )

            async def on_timeout():
                # Clear cache
                self.refuseable_tasks_cache.pop(user_id, None)
                # Update message to indicate expiration
                try:
                    await interaction.edit_original_response(
                        content="This task selection menu has expired. Please run /pf_refuse again.",
                        view=None  # This removes the select menu
                    )
                except Exception as e:
                    logger.error(f"Failed to update message on timeout: {e}")          

            # Attach the callback to the select element
            select.callback = select_callback

            # Create a view and add the select element to it
            view = View(timeout=self.cache_timeout)
            view.add_item(select)
            view.on_timeout = on_timeout

            # Send the message with the dropdown menu
            await interaction.followup.send("Please choose a task to refuse:", view=view, ephemeral=ephemeral_setting)

        @self.tree.command(name="pf_initial_verification", description="Submit a task for verification")
        async def pf_submit_for_verification(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /store_seed before submitting a task for verification.", 
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]

            logger.debug(f"TaskNodeDiscordBot.pf_initial_verification: Spawning wallet to fetch tasks for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            # Fetch account history
            memo_history = await self.generic_pft_utilities.get_account_memo_history(wallet.address)

            # Fetch accepted tasks
            accepted_tasks = await self.user_task_parser.get_accepted_proposals(account=memo_history)

            # Return if no accepted tasks
            if accepted_tasks.empty:
                await interaction.followup.send("You have no tasks to submit for verification.", ephemeral=ephemeral_setting)
                return

            # Cache the accepted tasks
            self.accepted_tasks_cache[user_id] = accepted_tasks

            # Create dropdown options based on the accepted tasks
            options = [
                SelectOption(
                    label=task_id, 
                    description=str(accepted_tasks.loc[task_id, 'proposal'])[:100], 
                    value=task_id
                )
                for task_id in accepted_tasks.index
            ]

            # Create the Select menu
            select = Select(placeholder="Choose a task to submit for verification", options=options)

            # Define the callback for when a user selects an option
            async def select_callback(interaction: discord.Interaction):
                selected_task_id = select.values[0]
                cached_tasks = self.accepted_tasks_cache[user_id]
                task_text = str(cached_tasks.loc[selected_task_id, 'proposal'])
                self.accepted_tasks_cache.pop(user_id, None)

                await interaction.response.send_modal(
                    CompletionModal(
                        task_id=selected_task_id,
                        task_text=task_text,
                        seed=seed,
                        user_name=interaction.user.name,
                        tasknode_utilities=self.tasknode_utilities,
                        ephemeral_setting=ephemeral_setting
                    )
                )

            async def on_timeout():
                # Clear cache
                self.accepted_tasks_cache.pop(user_id, None)
                # Update message to indicate expiration
                try:
                    await interaction.edit_original_response(
                        content="This task selection menu has expired. Please run /pf_initial_verification again.",
                        view=None  # This removes the select menu
                    )
                except Exception as e:
                    logger.error(f"Failed to update message on timeout: {e}")

            # Attach the callback to the select element
            select.callback = select_callback

            # Create a view and add the select element to it
            view = View(timeout=self.cache_timeout)
            view.add_item(select)
            view.on_timeout = on_timeout

            await interaction.followup.send("Please choose a task to submit for verification:", view=view, ephemeral=ephemeral_setting)

        @self.tree.command(name="pf_final_verification", description="Submit final verification for a task")
        async def pf_final_verification(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /store_seed before submitting final verification.", 
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]
            logger.debug(f"TaskNodeDiscordBot.pf_final_verification: Spawning wallet to fetch tasks for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            # Fetch account history
            memo_history = await self.generic_pft_utilities.get_account_memo_history(wallet.address)

            # Fetch verification tasks
            verification_tasks = await self.user_task_parser.get_verification_proposals(account=memo_history)
            
            # If there are no tasks in the verification queue, notify the user
            if verification_tasks.empty:
                await interaction.followup.send("You have no tasks pending final verification.", ephemeral=ephemeral_setting)
                return

            # Cache the verification tasks
            self.verification_tasks_cache[user_id] = verification_tasks

            # Create dropdown options based on the tasks in the verification queue
            options = [
                SelectOption(
                    label=task_id, 
                    description=str(verification_tasks.loc[task_id, 'verification'])[:100], 
                    value=task_id
                )
                for task_id in verification_tasks.index
            ]

            # Create the Select menu
            select = Select(placeholder="Choose a task to submit for final verification", options=options)

            # Define the callback for when a user selects an option
            async def select_callback(interaction: discord.Interaction):
                selected_task_id = select.values[0]
                cached_tasks = self.verification_tasks_cache[user_id]                
                task_text = str(cached_tasks.loc[selected_task_id, 'verification'])
                self.verification_tasks_cache.pop(user_id, None)

                # Open the modal to get the verification justification with the task text pre-populated
                await interaction.response.send_modal(
                    VerificationModal(
                        task_id=selected_task_id,
                        task_text=task_text,
                        seed=seed,
                        user_name=interaction.user.name,
                        tasknode_utilities=self.tasknode_utilities,
                        ephemeral_setting=ephemeral_setting
                    )
                )

            async def on_timeout():
                # Clear cache
                self.verification_tasks_cache.pop(user_id, None)
                # Update message to indicate expiration
                try:
                    await interaction.edit_original_response(
                        content="This task selection menu has expired. Please run /pf_final_verification again.",
                        view=None  # This removes the select menu
                    )
                except Exception as e:
                    logger.error(f"Failed to update message on timeout: {e}")

            # Attach the callback to the select element
            select.callback = select_callback

            # Create a view and add the select element to it
            view = View(timeout=self.cache_timeout)
            view.add_item(select)
            view.on_timeout = on_timeout

            # Send the message with the dropdown menu
            await interaction.followup.send("Please choose a task for final verification:", view=view, ephemeral=ephemeral_setting)

        @self.tree.command(name="pf_rewards", description="Show your recent Post Fiat rewards")
        async def pf_rewards(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before viewing rewards.",
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]
            logger.debug(f"TaskNodeDiscordBot.pf_rewards: Spawning wallet to fetch rewards for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            try:
                memo_history = await self.generic_pft_utilities.get_account_memo_history(wallet.address)
                memo_history = memo_history.sort_values('datetime')

                # Return immediately if memo history is empty
                if memo_history.empty:
                    await interaction.followup.send("You have no rewards to show.", ephemeral=ephemeral_setting)
                    return

                reward_summary_map = self.get_reward_data(all_account_info=memo_history)
                recent_rewards = self.format_reward_summary(reward_summary_map['reward_summaries'].tail(10))

                await self.send_long_interaction_response(
                    interaction=interaction,
                    message=recent_rewards,
                    ephemeral=ephemeral_setting
                )

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_rewards: An error occurred while fetching your rewards: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred while fetching your rewards: {str(e)}", ephemeral=True)

        @self.tree.command(name="pf_log", description="Send a long message to the remembrancer wallet")
        async def pf_remembrancer(interaction: discord.Interaction, message: str, encrypt: bool = False):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)
            
            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before using this command.",
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]
            user_name = interaction.user.name
            logger.debug(f"TaskNodeDiscordBot.pf_remembrancer: Spawning wallet to send message to remembrancer for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            try:

                message_obj = await interaction.followup.send(
                    "Sending message to remembrancer...",
                    ephemeral=ephemeral_setting,
                    wait=True  # returns message object
                )

                if encrypt:
                    handshake_success, user_key, counterparty_key, message_obj = await self._ensure_handshake(
                        interaction=interaction,
                        seed=seed,
                        counterparty=self.remembrancer,
                        username=user_name,
                        command_name="pf_remembrancer",
                        message_obj=message_obj
                    )
                    if not handshake_success:
                        return
                    
                    await message_obj.edit(content=f"Handshake verified. Proceeding to send message {message}...")

                try:
                    response = await self.generic_pft_utilities.send_memo(
                        wallet_seed_or_wallet=wallet,
                        username=user_name,
                        destination=self.remembrancer,
                        memo=message,
                        chunk=True,
                        compress=True,
                        encrypt=encrypt
                    )

                    if not self.generic_pft_utilities.verify_transaction_response(response):
                        raise Exception(f"Failed to send message to remembrancer: {response.result}")

                except Exception as e:
                    logger.error(f"TaskNodeDiscordBot.pf_remembrancer: Error sending memo: {e}")
                    logger.error(traceback.format_exc())
                    await message_obj.edit(content=f"An error occurred while sending the message: {str(e)}")
                    return

                response = response[-1] if isinstance(response, list) else response

                transaction_info = self.generic_pft_utilities.extract_transaction_info_from_response_object(
                    response=response
                )
                clean_string = transaction_info['clean_string']

                mode = "Encrypted message" if encrypt else "Message"
                await message_obj.edit(
                    content=f"Post Fiat Log: {message[:100]}...\n{mode} sent to remembrancer successfully. Last chunk details:\n{clean_string[:100]}..."
                )

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_remembrancer: An error occurred while sending the message: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(f"An error occurred while sending the message: {str(e)}", ephemeral=True)
        
        @self.tree.command(name="pf_chart", description="Generate a chart of your PFT rewards and metrics")
        async def pf_chart(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            await interaction.response.defer(ephemeral=ephemeral_setting)

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "You must store a seed using /pf_store_seed before generating a chart.", 
                    ephemeral=ephemeral_setting
                )
                return

            seed = self.user_seeds[user_id]
            logger.debug(f"TaskNodeDiscordBot.setup_hook.pf_chart: Spawning wallet to generate chart for {interaction.user.name}")
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)

            # Check initiation status
            if not await self.tasknode_utilities.has_initiation_rite(wallet_address=wallet.address):
                await interaction.followup.send(
                    "You must perform the initiation rite first. Run /pf_initiate to do so.", 
                    ephemeral=ephemeral_setting
                )
                return

            try:
                # Call the charting function
                await self.tasknode_utilities.output_pft_KPI_graph_for_address(user_wallet=wallet.address)
                
                # Create the file object from the saved image
                chart_file = discord.File(f'pft_rewards__{wallet.address}.png', filename='pft_chart.png')
                
                # Create an embed for better formatting
                embed = discord.Embed(
                    title="PFT Rewards Analysis",
                    color=discord.Color.blue()
                )
                
                # Add the chart image to the embed
                embed.set_image(url="attachment://pft_chart.png")
                
                # Send the embed with the chart
                await interaction.followup.send(
                    file=chart_file,
                    embed=embed,
                    ephemeral=ephemeral_setting
                )
                
                # Clean up the file after sending
                import os
                os.remove(f'pft_rewards__{wallet.address}.png')

            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_chart: An error occurred while generating your PFT chart: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while generating your PFT chart: {str(e)}", 
                    ephemeral=True
                )

        @self.tree.command(
            name="pf_leaderboard", 
            description="Display the Post Fiat Foundation Node Leaderboard"
        )
        async def pf_leaderboard(interaction: discord.Interaction):
            # Check if the user has permission (matches the specific ID)
            if interaction.user.id not in DISCORD_SUPER_USER_IDS:
                await interaction.response.send_message(
                    "You don't have permission to use this command.", 
                    ephemeral=True
                )
                return
                
            # Proceed with the command for authorized user
            await interaction.response.defer(ephemeral=False)
            
            try:
                # Generate and format the leaderboard
                leaderboard_df = await self.output_postfiat_foundation_node_leaderboard_df()
                self.format_and_write_leaderboard()
                
                embed = discord.Embed(
                    title="Post Fiat Foundation Node Leaderboard 🏆",
                    description=f"Current Post Fiat Leaderboard",
                    color=0x00ff00
                )
                
                file = discord.File("test_leaderboard.png", filename="leaderboard.png")
                embed.set_image(url="attachment://leaderboard.png")
                
                await interaction.followup.send(
                    embed=embed, 
                    file=file
                )
                
                # Clean up
                import os
                os.remove("test_leaderboard.png")
                
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.pf_leaderboard: An error occurred while generating the leaderboard: {str(e)}")
                logger.error(traceback.format_exc())
                await interaction.followup.send(
                    f"An error occurred while generating the leaderboard: {str(e)}"
                )

        # Sync the commands
        await self.tree.sync(guild=guild)
        logger.debug(f"TaskNodeDiscordBot.setup_hook: Slash commands synced")

        commands = await self.tree.fetch_commands(guild=guild)
        logger.debug(f"Registered commands: {[cmd.name for cmd in commands]}")

    async def _ensure_handshake(
        self,
        interaction: discord.Interaction,
        seed: str,
        counterparty: str,
        username: str,
        command_name: str,
        message_obj: discord.Message = None
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Ensures handshake protocol is established between wallet and counterparty

        Args: 
            interaction: Discord interaction object
            wallet_address: Wallet address of the user
            counterparty: Counterparty address
            seed: Seed of the user
            username: Username of the user
            command_name: Name of the command that requires the handshake protocol
            message_obj: Message object to edit (optional)

        Returns:
            tuple[bool, str, str, discord.Message]: (success, user_key, counterparty_key, message_obj)
        """
        # Transaction verification parameters from the user's perspective
        NODE_HANDSHAKE_RESPONSE_USER_VERIFICATION_ATTEMPTS = 24
        NODE_HANDSHAKE_RESPONSE_USER_VERIFICATION_INTERVAL = 5  # in seconds

        try:
            # Send message if we don't have a message object
            if not message_obj:
                message_obj = await interaction.followup.send(
                    "Checking encryption handshake status...",
                    ephemeral=True,
                    wait=True  # returns message object
                )

            # Check handshake status
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
            user_key, counterparty_key = await self.generic_pft_utilities.get_handshake_for_address(
                channel_address=wallet.classic_address,
                channel_counterparty=counterparty
            )

            if not user_key:
                # Send handshake if we haven't yet
                logger.debug(f"TaskNodeDiscordBot.{command_name}: Initiating handshake for {username} with {counterparty}")
                await self.generic_pft_utilities.send_handshake(
                    wallet_seed=seed,
                    destination=counterparty,
                    username=username
                )
                await message_obj.edit(content="Encryption handshake initiated. Waiting for onchain confirmation...")

                # Verify handshake completion and response from counterparty (node or remembrancer)
                for attempt in range(NODE_HANDSHAKE_RESPONSE_USER_VERIFICATION_ATTEMPTS):
                    logger.debug(f"TaskNodeDiscordBot.{command_name}: Checking handshake status for {username} with {counterparty} (attempt {attempt+1})")

                    user_key, counterparty_key = await self.generic_pft_utilities.get_handshake_for_address(
                        channel_address=wallet.classic_address,
                        channel_counterparty=counterparty
                    )

                    if counterparty_key:
                        logger.debug(f"TaskNodeDiscordBot.{command_name}: Handshake confirmed for {username} with {counterparty}")
                        break

                    if user_key:
                        await message_obj.edit(content="Handshake sent. Waiting for node to process...")

                    await asyncio.sleep(NODE_HANDSHAKE_RESPONSE_USER_VERIFICATION_INTERVAL)

            if not user_key:
                await message_obj.edit(content="Encryption handshake failed to send. Please reach out to support.")
                return False, None, None, message_obj
            
            if not counterparty_key:
                await message_obj.edit(content="Encryption handshake sent but not yet processed. Please wait and try again later.")
                return False, user_key, None, message_obj
            
            await message_obj.edit(content="Handshake verified. Proceeding with operation...")
            return True, user_key, counterparty_key, message_obj

        except Exception as e:
            logger.error(f"TaskNodeDiscordBot.{command_name}: An error occurred while ensuring handshake: {str(e)}")
            await message_obj.edit(content=f"An error occurred during handshake setup: {str(e)}")
            return False, None, None, message_obj

    async def on_ready(self):
        logger.debug(f'TaskNodeDiscordBot.on_ready: Logged in as {self.user} (ID: {self.user.id})')
        logger.debug('TaskNodeDiscordBot.on_ready: ------------------------------')
        logger.debug('TaskNodeDiscordBot.on_ready: Connected to the following guilds:')
        for guild in self.guilds:
            logger.debug(f'- {guild.name} (ID: {guild.id})')

    async def _split_message_into_chunks(self, content: str, max_chunk_size: int = 1900) -> list[str]:
        """Split a message into chunks that fit within Discord's message limit.
        
        Args:
            content: The message content to split
            max_chunk_size: Maximum size for each chunk (default: 1900 to leave room for formatting)
            
        Returns:
            List of message chunks
        """
        chunks = []
        while content:
            if len(content) <= max_chunk_size:
                chunks.append(content)
                break
                
            # Find the last space within the limit to avoid splitting words
            split_index = content[:max_chunk_size].rfind(' ')
            if split_index == -1:  # No space found, force split at max length
                split_index = max_chunk_size
                
            chunks.append(content[:split_index])
            content = content[split_index:].lstrip()  # Remove leading whitespace
            
        return chunks
    
    async def _format_chunk(self, chunk: str, code_block: bool = False) -> str:
        """Format a message chunk with optional code block formatting.
        
        Args:
            chunk: The message chunk to format
            code_block: Whether to wrap the chunk in a code block
            
        Returns:
            Formatted message chunk
        """
        if code_block:
            return f"```\n{chunk}\n```"
        return chunk
    
    async def _send_long_message(
        self,
        content: str,
        *,
        channel: Optional[discord.abc.GuildChannel] = None,
        message: Optional[discord.Message] = None,
        interaction: Optional[discord.Interaction] = None,
        code_block: bool = False,
        ephemeral: bool = True,
        mention_author: bool = True,
        delete_after: Optional[int] = None
    ) -> list[discord.Message]:
        """Send a long message, splitting it into chunks if necessary.
        
        Args:
            content: The message content to send
            channel: Discord channel to send to (optional)
            message: Original message to reply to (optional) 
            interaction: Discord interaction to respond to (optional)
            code_block: Whether to wrap chunks in code blocks
            ephemeral: Whether interaction responses should be ephemeral
            mention_author: Whether to mention author in replies
            delete_after: Number of seconds after which to delete messages
            
        Returns:
            List of sent messages
        """
        sent_messages = []
        chunks = await self._split_message_into_chunks(content)
        
        for chunk in chunks:
            formatted_chunk = await self._format_chunk(chunk, code_block)
            
            try:
                if interaction:
                    # For slash commands
                    await interaction.followup.send(formatted_chunk, ephemeral=ephemeral)
                elif channel:
                    # For direct channel messages
                    sent = await channel.send(formatted_chunk)
                    sent_messages.append(sent)
                elif message:
                    # For message replies
                    sent = await message.reply(formatted_chunk, mention_author=mention_author)
                    sent_messages.append(sent)
                else:
                    raise ValueError("Must provide either channel, message, or interaction")
                    
            except discord.errors.HTTPException as e:
                logger.error(f"Error sending message chunk: {e}")
                continue
                
        if delete_after and sent_messages:
            await asyncio.sleep(delete_after)
            for sent in sent_messages:
                try:
                    await sent.delete()
                except discord.errors.NotFound:
                    pass  # Message already deleted
                    
            if message:
                try:
                    await message.delete()
                except discord.errors.NotFound:
                    pass  # Original message already deleted
                    
        return sent_messages

    # Maintaining old methods for compatibility
    # TODO: Refactor to use _send_long_message directly
    async def send_long_message_to_channel(self, channel, long_message):
        return await self._send_long_message(long_message, channel=channel)

    async def send_long_interaction_response(self, interaction: discord.Interaction, message: str, ephemeral: bool = True):
        return await self._send_long_message(
            message,
            interaction=interaction,
            code_block=True,
            ephemeral=ephemeral
        )
        
    async def send_long_message(self, message, long_message):
        return await self._send_long_message(
            content=long_message,
            message=message,
            mention_author=True
        )
    async def send_long_message_then_delete(self, message, long_message, delete_after):
        return await self._send_long_message(
            long_message,
            message=message,
            delete_after=delete_after
        )

    async def send_long_escaped_message(self, message, long_message):
        return await self._send_long_message(
            long_message,
            message=message,
            code_block=True,
            mention_author=True
        )

    async def transaction_notifier(self):
        await self.wait_until_ready()
        channel = self.get_channel(self.node_config.discord_activity_channel_id)

        if not channel:
            logger.error(
                f"TaskNodeDiscordBot.transaction_notifier: Channel with ID "
                f"{self.node_config.discord_activity_channel_id} not found"
            )
            return

        while not self.is_closed():
            try:
                result = await self.notification_queue.get()
                message = self.format_notification(result)
                await channel.send(message)
            except Exception as e:
                logger.error(f"Error processing notification: {str(e)}")
                logger.error(traceback.format_exc())
            
            await asyncio.sleep(0.5)  # Prevent spam

    def format_notification(self, tx: Dict[str, Any]) -> str:
        """Format the reviewing result for Discord"""
        url = self.network_config.explorer_tx_url_mask.format(hash=tx['hash'])
        
        return (
            f"Date: {tx['datetime']}\n"
            f"Account: `{tx['account']}`\n"
            f"Memo Format: `{tx['memo_format']}`\n"
            f"Memo Type: `{tx['memo_type']}`\n"
            f"Memo Data: `{tx['memo_data']}`\n"
            f"PFT: {tx.get('pft_absolute_amount', 0)}\n"
            f"URL: {url}"
        )

    async def death_march_checker_for_user(self, user_id: int):
        """Individual death march checker for a single user."""
        settings = self.user_deathmarch_settings[user_id]

        while not self.is_closed():
            now_utc = datetime.now(timezone.utc)

            # Check if death march has ended
            if now_utc >= settings.session_end:
                logger.debug(f"death_march_checker: user_id={user_id} - Death March ended; clearing session.")
                settings.session_start = None
                settings.session_end = None
                settings.channel_id = None
                settings.last_checkin = None
                break

            # Check local time window
            user_tz = pytz.timezone(settings.timezone)
            now_local = datetime.now(user_tz).time()

            if settings.start_time <= now_local <= settings.end_time:
                # Check if enough time has passed since last check-in
                if settings.last_checkin:
                    time_since_last = (now_utc - settings.last_checkin).total_seconds() / 60
                    if time_since_last < settings.check_interval:
                        await asyncio.sleep((settings.check_interval - time_since_last) * 60)
                        continue
                logger.debug(f"death_march_checker: user_id={user_id} is within time window and due for check-in.")
                try:
                    seed = self.user_seeds.get(user_id)
                    if not seed:
                        logger.debug(f"death_march_checker: user_id={user_id} has no stored seed; ending death march.")
                        break

                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    analyzer = await ODVFocusAnalyzer.create(
                        account_address=user_wallet.classic_address,
                        openrouter=self.openrouter_tool,
                        user_context_parser=self.user_task_parser,
                        pft_utils=self.generic_pft_utilities
                    )
                    focus_text = await analyzer.get_response_async("Death March Check-In")

                    channel = self.get_channel(settings.channel_id)
                    if channel:
                        # Mention the user
                        mention_string = f"<@{user_id}>"
                        await channel.send(f"{mention_string} **Death March Check-In**\n{focus_text}")
                        settings.last_checkin = now_utc
                    else:
                        logger.warning(
                            f"death_march_checker: Channel {settings.channel_id} not found for user_id={user_id}."
                        )
                        break
                except Exception as e:
                    logger.error(
                        f"death_march_checker: Error processing user_id={user_id}: {str(e)}"
                    )

            # Sleep until next interval
            await asyncio.sleep(settings.check_interval * 60)

        logger.info(f"death_march_checker: Ending death march loop for user_id={user_id}")

    async def death_march_reminder(self):
        await self.wait_until_ready()
        
        # Wait for 10 seconds after server start (for testing, change back to 30 minutes in production)
        await asyncio.sleep(30)
        
        target_user_id = 402536023483088896  # The specific user ID
        channel_id = 1229917290254827521  # The specific channel ID
        
        est_tz = pytz.timezone('US/Eastern')
        start_time = time(6, 30)  # 6:30 AM
        end_time = time(21, 0)  # 9:00 PM
        
        while not self.is_closed():
            try:
                now = datetime.now(est_tz).time()
                if start_time <= now <= end_time:
                    channel = self.get_channel(channel_id)
                    if channel:
                        if target_user_id in self.user_seeds:
                            seed = self.user_seeds[target_user_id]
                            logger.debug(f"TaskNodeDiscordBot.death_march_reminder: Spawning wallet to fetch info for {target_user_id}")
                            user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                            user_address = user_wallet.classic_address
                            tactical_string = await self.tasknode_utilities.get_o1_coaching_string_for_account(user_address)
                            
                            # Send the message to the channel
                            await self.send_long_message_to_channel(channel, f"<@{target_user_id}> Death March Update:\n{tactical_string}")
                        else:
                            logger.debug(f"TaskNodeDiscordBot.death_march_reminder: No seed found for user {target_user_id}")
                    else:
                        logger.debug(f"TaskNodeDiscordBot.death_march_reminder: Channel with ID {channel_id} not found")
                else:
                    logger.debug("TaskNodeDiscordBot.death_march_reminder: Outside of allowed time range. Skipping Death March reminder.")
            except Exception as e:
                logger.error(f"TaskNodeDiscordBot.death_march_reminder: An error occurred: {str(e)}")

            # Wait for 30 minutes before the next reminder (10 seconds for testing)
            await asyncio.sleep(30*60)  # Change to 1800 (30 minutes) for production
            
    async def on_message(self, message):
        if message.author.id == self.user.id:
            return

        user_id = message.author.id
        if user_id not in self.conversations:
            self.conversations[user_id] = []

        self.conversations[user_id].append({
            "role": "user",
            "content": message.content})

        conversation = self.conversations[user_id]
        if len(self.conversations[user_id]) > global_constants.MAX_HISTORY:
            del self.conversations[user_id][0]  # Remove the oldest message

        if message.content.startswith('!odv'):
            
            system_content_message = [{"role": "system", "content": odv_system_prompt}]
            ref_convo = system_content_message + conversation
            api_args = {
            "model": self.default_openai_model,
            "messages": ref_convo}
            op_df = self.openai_request_tool.create_writable_df_for_chat_completion(api_args=api_args)
            content = op_df['choices__message__content'][0]
            gpt_response = content
            self.conversations[user_id].append({
                "role": 'system',
                "content": gpt_response})
        
            await self.send_long_message(message, gpt_response)

        if message.content.startswith('!tactics'):
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                try:
                    logger.debug(f"TaskNodeDiscordBot.tactics: Spawning wallet to fetch info for {message.author.name}")
                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    memo_history = await self.generic_pft_utilities.get_account_memo_history(user_wallet.classic_address)
                    full_user_context = await self.user_task_parser.get_full_user_context_string(user_wallet.classic_address, memo_history=memo_history)
                    
                    openai_request_tool = OpenAIRequestTool()
                    
                    user_prompt = f"""You are ODV Tactician module.
                    The User has the following transaction context as well as strategic context
                    they have uploaded here
                    <FULL USER CONTEXT STARTS HERE>
                    {full_user_context}
                    <FULL USER CONTEXT ENDS HERE>
                    Your job is to read through this and to interogate the future AI as to the best, very short-term use of the user's time.
                    You are to condense this short term use of the user's time down to a couple paragraphs at most and provide it
                    to the user
                    """
                    
                    api_args = {
                        "model": global_constants.DEFAULT_OPEN_AI_MODEL,
                        "messages": [
                            {"role": "system", "content": odv_system_prompt},
                            {"role": "user", "content": user_prompt}
                        ]
                    }
                    
                    writable_df = openai_request_tool.create_writable_df_for_chat_completion(api_args=api_args)
                    tactical_string = writable_df['choices__message__content'][0]
                    
                    await self.send_long_message(message, tactical_string)
            
                except Exception as e:
                    error_message = f"An error occurred while generating tactical advice: {str(e)}"
                    await message.reply(error_message, mention_author=True)
            else:
                await message.reply("You must store a seed using /pf_store_seed before getting tactical advice.", mention_author=True)

# Add this in the on_message handler section of your Discord bot

        if message.content.startswith('!coach'):
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                try:
                    # Get user's wallet address
                    logger.debug(f"TaskNodeDiscordBot.coach: Spawning wallet to fetch info for {message.author.name}")
                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    wallet_address = user_wallet.classic_address

                    # Check PFT balance
                    pft_balance = await self.generic_pft_utilities.fetch_pft_balance(wallet_address)
                    logger.debug(f"TaskNodeDiscordBot.coach: PFT balance for {message.author.name} is {pft_balance}")
                    if not (RuntimeConfig.USE_TESTNET and RuntimeConfig.DISABLE_PFT_REQUIREMENTS):
                        if pft_balance < 25000:
                            await message.reply(
                                f"You need at least 25,000 PFT to use the coach command. Your current balance is {pft_balance:,.2f} PFT.", 
                                mention_author=True
                            )
                            return

                    # Get user's full context
                    memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=wallet_address)
                    full_context = await self.user_task_parser.get_full_user_context_string(account_address=wallet_address, memo_history=memo_history)
                    
                    # Get chat history
                    chat_history = []
                    if user_id in self.conversations:
                        chat_history = [
                            f"{msg['role'].upper()}: {msg['content']}"
                            for msg in self.conversations[user_id][-10:]  # Get last 10 messages
                        ]
                    formatted_chat = "\n".join(chat_history)

                    # Get the user's specific question/request
                    user_query = message.content.replace('!coach', '').strip()
                    if not user_query:
                        user_query = "Please provide coaching based on my current context and history."

                    # Create the user prompt
                    user_prompt = f"""Based on the following context about me, please provide coaching and guidance.
Rules of engagement:
1. Take the role of a Tony Robbins Type highly paid executive coach while also fulfilling the ODV mandate
2. The goal is to deliver a high NLP score to the user, or to neurolinguistically program them to be likely to fulfill the mandate
provided
3. Keep your advice succinct enough to get the job done but long enough to fully respond to the advice
4. Have the frame that the user is paying 10% or more of their annual earnings to you so your goal is to MAXIMIZE 
the user's earnings and therefore ability to pay you for advice

FULL USER CONTEXT:
{full_context}

RECENT CHAT HISTORY:
{formatted_chat}

My specific question/request is: {user_query}"""

                    # Add reaction to show processing
                    await message.add_reaction('⏳')

                    # Make the API call using o1_preview_simulated_request
                    response = await self.openai_request_tool.o1_preview_simulated_request_async(
                        system_prompt=odv_system_prompt,
                        user_prompt=user_prompt
                    )
                    
                    # Extract content from response
                    content = response.choices[0].message.content
                    
                    # Store the response in conversation history
                    self.conversations[user_id].append({
                        "role": 'assistant',
                        "content": content
                    })
                    
                    # Remove the processing reaction
                    await message.remove_reaction('⏳', self.user)
                    
                    # Send the response
                    await self.send_long_message(message, content)
                    
                except Exception as e:
                    await message.remove_reaction('⏳', self.user)
                    logger.error(f"TaskNodeDiscordBot.coach: An error occurred while processing your request: {str(e)}")
                    logger.error(traceback.format_exc())
                    error_msg = f"An error occurred while processing your request: {str(e)}"
                    await message.reply(error_msg, mention_author=True)
            else:
                await message.reply("You must store a seed using !store_seed before using the coach.", mention_author=True)

        if message.content.startswith('!blackprint'):
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                try:
                    logger.debug(f"TaskNodeDiscordBot.blackprint: Spawning wallet to fetch info for {message.author.name}")
                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    user_address = user_wallet.classic_address
                    tactical_string = await self.tasknode_utilities.generate_coaching_string_for_account(user_address)
                    
                    await self.send_long_message(message, tactical_string)
            
                except Exception as e:
                    error_message = f"An error occurred while generating tactical advice: {str(e)}"
                    await message.reply(error_message, mention_author=True)
            else:
                await message.reply("You must store a seed using /pf_store_seed before getting tactical advice.", mention_author=True)

        if message.content.startswith('!deathmarch'):
            user_id = message.author.id
            if user_id not in self.user_seeds:
                await message.reply("You must store a seed using /pf_store_seed first.", mention_author=True)
                return

            try:
                seed = self.user_seeds[user_id]
                user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                
                # Create the analyzer inline
                analyzer = await ODVFocusAnalyzer.create(
                    account_address=user_wallet.classic_address,
                    openrouter=self.openrouter_tool,
                    user_context_parser=self.user_task_parser,
                    pft_utils=self.generic_pft_utilities
                )
                # If you want the same exact prompt
                focus_text = analyzer.get_response("Death March Check-In")

                #await message.channel.send(f"**Death March Check-In**\n{focus_text}")
                await self.send_long_message(message, focus_text)

            except Exception as e:
                logger.error(f"!deathmarch: An error occurred for user {user_id}: {str(e)}")
                await message.reply(f"An error occurred: {str(e)}", mention_author=True)
    
        if message.content.startswith('!redpill'):
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                try:
                    logger.debug(f"TaskNodeDiscordBot.redpill: Spawning wallet to fetch info for {message.author.name}")
                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    user_address = user_wallet.classic_address
                    tactical_string = await self.tasknode_utilities.o1_redpill(user_address)
                    
                    await self.send_long_message(message, tactical_string)
            
                except Exception as e:
                    error_message = f"An error occurred while generating tactical advice: {str(e)}"
                    await message.reply(error_message, mention_author=True)
            else:
                await message.reply("You must store a seed using /pf_store_seed before getting tactical advice.", mention_author=True)

        if message.content.startswith('!docrewrite'):
            if user_id in self.user_seeds:
                seed = self.user_seeds[user_id]
                
                try:
                    logger.debug(f"TaskNodeDiscordBot.docrewrite: Spawning wallet to fetch info for {message.author.name}")
                    user_wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)
                    user_address = user_wallet.classic_address
                    tactical_string = await self.tasknode_utilities.generate_document_rewrite_instructions(user_address)
                    
                    await self.send_long_message(message, tactical_string)
            
                except Exception as e:
                    error_message = f"An error occurred while generating tactical advice: {str(e)}"
                    await message.reply(error_message, mention_author=True)
            else:
                await message.reply("You must store a seed using /pf_store_seed before getting tactical advice.", mention_author=True)

    async def generate_basic_balance_info_string(self, address: str, owns_wallet: bool = True) -> str:
        """Generate account information summary including balances and stats.
        
        Args:
            wallet: Either an XRPL wallet object (for full access including encrypted docs) 
                or an address string (for public info only)
                
        Returns:
            str: Formatted account information string
        """
        account_info = AccountInfo(address=address)

        # Get balances
        try:
            account_info.xrp_balance = await self.generic_pft_utilities.fetch_xrp_balance(address)
            account_info.pft_balance = await self.generic_pft_utilities.fetch_pft_balance(address)
        except Exception as e:
            # Account probably not activated yet
            account_info.xrp_balance = 0
            account_info.pft_balance = 0

        try:
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=address)

            if not memo_history.empty:

                # transaction count
                account_info.transaction_count = len(memo_history)

                # Likely username
                outgoing_memo_format = list(memo_history[memo_history['direction']=='OUTGOING']['memo_format'].mode())
                if len(outgoing_memo_format) > 0:
                    account_info.username = outgoing_memo_format[0]
                else:
                    account_info.username = "Unknown"

                # Reward statistics
                reward_data = self.get_reward_data(all_account_info=memo_history)
                if not reward_data['reward_ts'].empty:
                    account_info.monthly_pft_avg = float(reward_data['reward_ts'].tail(4).mean().iloc[0])
                    account_info.weekly_pft_avg = float(reward_data['reward_ts'].tail(1).mean().iloc[0])

            # Get google doc link
            if owns_wallet:
                account_info.google_doc_link = await self.user_task_parser.get_latest_outgoing_context_doc_link(address)

        except Exception as e:
            logger.error(f"Error generating account info for {address}: {e}")
            logger.error(traceback.format_exc())

        return self._format_account_info(account_info)
    
    def _format_account_info(self, info: AccountInfo) -> str:
        """Format AccountInfo into readable string."""
        output = f"""ACCOUNT INFO for {info.address}
                    LIKELY ALIAS:     {info.username}
                    XRP BALANCE:      {info.xrp_balance}
                    PFT BALANCE:      {info.pft_balance}
                    NUM PFT MEMO TX:  {info.transaction_count}
                    PFT MONTHLY AVG:  {info.monthly_pft_avg}
                    PFT WEEKLY AVG:   {info.weekly_pft_avg}"""
        
        if info.google_doc_link:
            output += f"\n\nCONTEXT DOC:      {info.google_doc_link}"
        
        return output
    
    def format_tasks_for_discord(self, input_text: str):
        """
        Format task list for Discord with proper formatting and emoji indicators.
        Handles three sections: NEW TASKS, ACCEPTED TASKS, and TASKS PENDING VERIFICATION.
        Returns a list of formatted chunks ready for Discord sending.
        """
        # Handle empty input
        if not input_text or input_text.strip() == "":
            return ["```ansi\n\u001b[1;33m=== TASK STATUS ===\u001b[0m\n\u001b[0;37mNo tasks found.\u001b[0m\n```"]

        # Split into sections
        sections = input_text.split('\n')
        current_section = None
        formatted_parts = []
        current_chunk = ["```ansi"]
        current_chunk_size = len(current_chunk[0])

        def add_to_chunks(content):
            nonlocal current_chunk, current_chunk_size
            content_size = len(content) + 1  # +1 for newline
            
            if current_chunk_size + content_size > 1900:
                current_chunk.append("```")
                formatted_parts.append("\n".join(current_chunk))
                current_chunk = ["```ansi"]
                current_chunk_size = len(current_chunk[0])
                
            current_chunk.append(content)
            current_chunk_size += content_size

        def format_task_id(task_id: str) -> tuple[str, str]:
            """Format task ID and extract date"""
            try:
                datetime_str = task_id.split('__')[0]
                date_obj = datetime.strptime(datetime_str, '%Y-%m-%d_%H:%M')
                formatted_date = date_obj.strftime('%d %b %Y %H:%M')
                return task_id, formatted_date
            except (ValueError, IndexError):
                return task_id, task_id
        
        # Process input text line by line
        task_data = {}
        for line in sections:
            line = line.strip()
            if not line:
                continue

            # Check for section headers
            if line in ["NEW TASKS", "ACCEPTED TASKS", "TASKS PENDING VERIFICATION"]:
                if current_section:  # Add spacing between sections
                    add_to_chunks("")
                current_section = line
                add_to_chunks(f"\u001b[1;33m=== {current_section} ===\u001b[0m")
                continue

            # Process task information
            if line.startswith("Task ID: "):
                task_id = line.replace("Task ID: ", "").strip()
                task_data = {"id": task_id}
                task_id, formatted_date = format_task_id(task_id)
                add_to_chunks(f"\u001b[1;36m📌 Task {task_id}\u001b[0m")
                add_to_chunks(f"\u001b[0;37mDate: {formatted_date}\u001b[0m")
                continue

            if line.startswith("Proposal: "):
                proposal = line.replace("Proposal: ", "").strip()
                proposal = proposal.replace("PROPOSED PF ___", "").strip()
                priority_match = re.search(r'\.\. (\d+)$', proposal)
                if priority_match:
                    priority = priority_match.group(1)
                    proposal = proposal.replace(f".. {priority}", "").strip()
                    add_to_chunks(f"\u001b[0;32mPriority: {priority}\u001b[0m")
                add_to_chunks(f"\u001b[1;37mProposal:\u001b[0m\n{proposal}")
                continue

            if line.startswith("Acceptance: "):
                acceptance = line.replace("Acceptance: ", "").strip()
                add_to_chunks(f"\u001b[1;37mAcceptance:\u001b[0m\n{acceptance}")
                continue

            if line.startswith("Verification Prompt: "):
                verification = line.replace("Verification Prompt: ", "").strip()
                add_to_chunks(f"\u001b[1;37mVerification Prompt:\u001b[0m\n{verification}")
                continue

            if line.startswith("-" * 10):  # Separator line
                add_to_chunks("─" * 50)
                continue

        # Finalize last chunk
        current_chunk.append("```")
        formatted_parts.append("\n".join(current_chunk))
        
        return formatted_parts
        
    def format_pending_tasks(self, pending_proposals_df):
        """
        Convert pending_proposals_df to a more legible string format for Discord.
        
        Args:
            pending_proposals_df: DataFrame containing pending proposals
            
        Returns:
            Formatted string representation of the pending proposals
        """
        formatted_tasks = []
        for idx, row in pending_proposals_df.iterrows():
            task_str = f"Task ID: {idx}\n"
            task_str += f"Proposal: {row['proposal']}\n"
            task_str += "-" * 50  # Separator
            formatted_tasks.append(task_str)
        
        formatted_task_string =  "\n".join(formatted_tasks)
        output_string="NEW TASKS\n" + formatted_task_string
        return output_string

    def format_accepted_tasks(self, accepted_proposals_df):
        """
        Convert accepted_proposals_df to a legible string format for Discord.
        
        Args:
            accepted_proposals_df: DataFrame containing outstanding tasks
            
        Returns:
            Formatted string representation of the tasks
        """
        formatted_tasks = []
        for idx, row in accepted_proposals_df.iterrows():
            task_str = f"Task ID: {idx}\n"
            task_str += f"Proposal: {row['proposal']}\n"
            task_str += f"Acceptance: {row['acceptance']}\n"
            task_str += "-" * 50  # Separator
            formatted_tasks.append(task_str)
        
        formatted_task_string =  "\n".join(formatted_tasks)
        output_string="ACCEPTED TASKS\n" + formatted_task_string
        return output_string
    
    def format_verification_tasks(self, verification_proposals_df):
        """
        Format the verification_requirements dataframe into a string.

        Args:
            verification_proposals_df (pd.DataFrame): DataFrame containing tasks pending verification

        Returns:
        str: Formatted string of verification requirements
        """
        formatted_output = "TASKS PENDING VERIFICATION\n"
        for idx, row in verification_proposals_df.iterrows():
            formatted_output += f"Task ID: {idx}\n"
            formatted_output += f"Proposal: {row['proposal']}\n"
            formatted_output += f"Verification Prompt: {row['verification']}\n"
            formatted_output += "-" * 50 + "\n"
        return formatted_output
    
    async def create_full_outstanding_pft_string(self, account_address):
        """ 
        This takes in an account address and outputs the current state of its outstanding tasks.
        Returns empty string for accounts with no PFT-related transactions.
        """ 
        memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=account_address, pft_only=True)
        if memo_history.empty:
            return ""
        
        memo_history.sort_values('datetime', inplace=True)
        pending_proposals = await self.user_task_parser.get_pending_proposals(account=memo_history)
        accepted_proposals = await self.user_task_parser.get_accepted_proposals(account=memo_history)
        verification_proposals = await self.user_task_parser.get_verification_proposals(account=memo_history)

        pending_string = self.format_pending_tasks(pending_proposals)
        accepted_string = self.format_accepted_tasks(accepted_proposals)
        verification_string = self.format_verification_tasks(verification_proposals)

        full_postfiat_outstanding_string=f"{pending_string}\n{accepted_string}\n{verification_string}"
        return full_postfiat_outstanding_string

    def _calculate_weekly_reward_totals(self, specific_rewards):
        """Calculate weekly reward totals with proper date handling.
        
        Returns DataFrame with weekly_total column indexed by date"""
        # Calculate daily totals
        daily_totals = specific_rewards[['directional_pft', 'datetime']].groupby('datetime').sum()

        if daily_totals.empty:
            logger.warning("No rewards data available to calculate weekly totals.")
            return pd.DataFrame(columns=['weekly_total'])

        # Extend date range to today
        today = pd.Timestamp.today().normalize()
        start_date = daily_totals.index.min()

        if pd.isna(start_date):
            logger.warning("Start date is NaT, cannot calculate weekly totals.")
            return pd.DataFrame(columns=['weekly_total'])
        
        date_range = pd.date_range(
            start=start_date,
            end=today,
            freq='D'
        )

        # Fill missing dates and calculate weekly totals
        extended_daily_totals = daily_totals.reindex(date_range, fill_value=0)
        extended_daily_totals = extended_daily_totals.resample('D').last().fillna(0)
        extended_daily_totals['weekly_total'] = extended_daily_totals.rolling(7).sum()

        # Return weekly totals
        weekly_totals = extended_daily_totals.resample('W').last()[['weekly_total']]
        weekly_totals.index.name = 'date'

        # if weekly totals are NaN, set them to 0
        weekly_totals = weekly_totals.fillna(0)

        return weekly_totals
    
    def _pair_rewards_with_tasks(self, specific_rewards, all_account_info):
        """Pair rewards with their original requests and proposals.
        
        Returns DataFrame with columns: memo_data, directional_pft, datetime, memo_type, request, proposal
        """
        # Get reward details
        reward_details = specific_rewards[
            ['memo_data', 'directional_pft', 'datetime', 'memo_type']
        ].sort_values('datetime')

        # Get original requests and proposals
        task_requests = all_account_info[
            all_account_info['memo_data'].apply(lambda x: TaskType.REQUEST_POST_FIAT.value in x)
        ].groupby('memo_type').first()['memo_data']

        proposal_patterns = TASK_PATTERNS[TaskType.PROPOSAL]
        task_proposals = all_account_info[
            all_account_info['memo_data'].apply(lambda x: any(pattern in str(x) for pattern in proposal_patterns))
        ].groupby('memo_type').first()['memo_data']

        # Map requests and proposals to rewards
        reward_details['request'] = reward_details['memo_type'].map(task_requests).fillna('No Request String')
        reward_details['proposal'] = reward_details['memo_type'].map(task_proposals)

        return reward_details

    def get_reward_data(self, all_account_info: pd.DataFrame):
        """Get reward time series and task completion history.
        
        Args:
            all_account_info: DataFrame containing account memo details
            
        Returns:
            dict with keys:
                - reward_ts: DataFrame of weekly reward totals
                - reward_summaries: DataFrame containing rewards paired with original requests/proposals
        """
        # Get basic reward data
        reward_responses = all_account_info[all_account_info['directional_pft'] > 0]
        specific_rewards = reward_responses[
            reward_responses.memo_data.apply(lambda x: "REWARD RESPONSE" in x)
        ]

        if specific_rewards.empty or len(specific_rewards) == 0:
            return {
                'reward_ts': pd.DataFrame(),
                'reward_summaries': pd.DataFrame()
            }

        # Get weekly totals
        weekly_totals = self._calculate_weekly_reward_totals(specific_rewards)

        # Get reward summaries with context
        reward_summaries = self._pair_rewards_with_tasks(
            specific_rewards=specific_rewards,
            all_account_info=all_account_info
        )

        return {
            'reward_ts': weekly_totals,
            'reward_summaries': reward_summaries
        }

    @staticmethod
    def format_reward_summary(reward_summary_df):
        """
        Convert reward summary dataframe into a human-readable string.
        :param reward_summary_df: DataFrame containing reward summary information
        :return: Formatted string representation of the rewards
        """
        formatted_rewards = []
        for _, row in reward_summary_df.iterrows():
            reward_str = f"Date: {row['datetime']}\n"
            reward_str += f"Request: {row['request']}\n"
            reward_str += f"Proposal: {row['proposal']}\n"
            reward_str += f"Reward: {row['directional_pft']} PFT\n"
            reward_str += f"Response: {row['memo_data'].replace(TaskType.REWARD.value, '')}\n"
            reward_str += "-" * 50  # Separator
            formatted_rewards.append(reward_str)
        
        output_string = "REWARD SUMMARY\n\n" + "\n".join(formatted_rewards)
        return output_string
        
    def get_all_transactions_for_active_wallets(self):
        """Get all transactions for active post fiat wallets (balance <= -2000)"""
        active_wallets = [
            account for account, data in self.generic_pft_utilities.get_pft_holders().items()
            if float(data['balance']) <= -2000
        ]
        
        transactions = asyncio.run(self.generic_pft_utilities.transaction_repository.get_active_wallet_transactions(active_wallets))
        
        return pd.DataFrame(transactions)

    def get_all_account_pft_memo_data(self):
        """Get all PFT memo data for computation of leaderboard."""
        all_transactions = self.get_all_transactions_for_active_wallets()
        
        if all_transactions.empty:
            return pd.DataFrame()

        # Filter for transactions with memos and non-zero PFT amounts
        memo_transactions = all_transactions[
            (all_transactions['memo_type'].notna()) & 
            (all_transactions['pft_amount'] != 0)
        ].copy()

        # Convert and clean up datetime
        memo_transactions['datetime'] = pd.to_datetime(
            memo_transactions['close_time_iso']
        ).dt.tz_localize(None)
        
        memo_transactions['datetime'] = memo_transactions['datetime'].dt.strftime('%Y-%m-%d')
        memo_transactions['datetime'] = pd.to_datetime(memo_transactions['datetime'])

        return memo_transactions

    def format_and_write_leaderboard(self):
        """ This loads the current leaderboard df and writes it"""

        def format_leaderboard_df(df):
            """
            Format the leaderboard DataFrame with cleaned up number formatting
            
            Args:
                df: pandas DataFrame with the leaderboard data
            Returns:
                formatted DataFrame with cleaned up number display
            """
            # Create a copy to avoid modifying the original
            formatted_df = df.copy()
            
            # Format total_rewards as whole numbers with commas
            def format_number(x):
                try:
                    # Try to convert directly to int
                    return f"{int(x):,}"
                except ValueError:
                    # If already formatted with commas, remove them and convert
                    try:
                        return f"{int(str(x).replace(',', '')):,}"
                    except ValueError:
                        return str(x)
            
            formatted_df['total_rewards'] = formatted_df['total_rewards'].apply(format_number)
            
            # Format yellow_flag_pct as percentage with 1 decimal place
            def format_percentage(x):
                try:
                    if pd.notnull(x):
                        # Remove % if present and convert to float
                        x_str = str(x).replace('%', '')
                        value = float(x_str)
                        if value > 1:  # Already in percentage form
                            return f"{value:.1f}%"
                        else:  # Convert to percentage
                            return f"{value*100:.1f}%"
                    return "0%"
                except ValueError:
                    return str(x)
            
            formatted_df['yellow_flag_pct'] = formatted_df['yellow_flag_pct'].apply(format_percentage)
            
            # Format reward_percentile with 1 decimal place
            def format_float(x):
                try:
                    return f"{float(str(x).replace(',', '')):,.1f}"
                except ValueError:
                    return str(x)
            
            formatted_df['reward_percentile'] = formatted_df['reward_percentile'].apply(format_float)
            
            # Format score columns with 1 decimal place
            score_columns = ['focus', 'motivation', 'efficacy', 'honesty', 'total_qualitative_score']
            for col in score_columns:
                formatted_df[col] = formatted_df[col].apply(lambda x: f"{float(x):.1f}" if pd.notnull(x) and x != 'N/A' else "N/A")
            
            # Format overall_score with 1 decimal place
            formatted_df['overall_score'] = formatted_df['overall_score'].apply(format_float)
            
            return formatted_df
        
    async def output_postfiat_foundation_node_leaderboard_df(self):
        """ This generates the full Post Fiat Foundation Leaderboard """ 
        all_accounts = self.get_all_account_pft_memo_data()
        # Get the mode (most frequent) memo_format for each account
        account_modes = all_accounts.groupby('account')['memo_format'].agg(lambda x: x.mode()[0]).reset_index()
        # If you want to see the counts as well to verify
        account_counts = all_accounts.groupby(['account', 'memo_format']).size().reset_index(name='count')
        
        # Sort by account for readability
        account_modes = account_modes.sort_values('account')
        account_name_map = account_modes.groupby('account').first()['memo_format']
        past_month_transactions = all_accounts[all_accounts['datetime']>datetime.now()-datetime.timedelta(30)]
        node_transactions = past_month_transactions[past_month_transactions['account']==self.generic_pft_utilities.node_address].copy()
        rewards_only=node_transactions[node_transactions['memo_data'].apply(lambda x: TaskType.REWARD.value in str(x))].copy()
        rewards_only['count']=1
        rewards_only['PFT']=rewards_only['tx_json'].apply(lambda x: x['DeliverMax']['value']).astype(float)
        account_to_yellow_flag__count = rewards_only[rewards_only['memo_data'].apply(lambda x: 'YELLOW FLAG' in x)][['count','destination']].groupby('destination').sum()['count']
        account_to_red_flag__count = rewards_only[rewards_only['memo_data'].apply(lambda x: 'RED FLAG' in x)][['count','destination']].groupby('destination').sum()['count']
        
        total_reward_number= rewards_only[['count','destination']].groupby('destination').sum()['count']
        account_score_constructor = pd.DataFrame(account_name_map)
        account_score_constructor=account_score_constructor[account_score_constructor.index!=self.generic_pft_utilities.node_address].copy()
        account_score_constructor['reward_count']=total_reward_number
        account_score_constructor['yellow_flags']=account_to_yellow_flag__count
        account_score_constructor=account_score_constructor[['reward_count','yellow_flags']].fillna(0).copy()
        account_score_constructor= account_score_constructor[account_score_constructor['reward_count']>=1].copy()
        account_score_constructor['yellow_flag_pct']=account_score_constructor['yellow_flags']/account_score_constructor['reward_count']
        total_pft_rewards= rewards_only[['destination','PFT']].groupby('destination').sum()['PFT']
        account_score_constructor['red_flag']= account_to_red_flag__count
        account_score_constructor['red_flag']=account_score_constructor['red_flag'].fillna(0)
        account_score_constructor['total_rewards']= total_pft_rewards
        account_score_constructor['reward_score__z']=(account_score_constructor['total_rewards']-account_score_constructor['total_rewards'].mean())/account_score_constructor['total_rewards'].std()
        
        account_score_constructor['yellow_flag__z']=(account_score_constructor['yellow_flag_pct']-account_score_constructor['yellow_flag_pct'].mean())/account_score_constructor['yellow_flag_pct'].std()
        account_score_constructor['quant_score']=(account_score_constructor['reward_score__z']*.65)-(account_score_constructor['reward_score__z']*-.35)
        top_score_frame = account_score_constructor[['total_rewards','yellow_flag_pct','quant_score']].sort_values('quant_score',ascending=False)
        top_score_frame['account_name']=account_name_map
        user_account_map = {}
        for x in list(top_score_frame.index):
            memo_history = await self.generic_pft_utilities.get_account_memo_history(account_address=x)
            user_account_string = await self.user_task_parser.get_full_user_context_string(account_address=x, memo_history=memo_history)
            logger.debug(x)
            user_account_map[x]= user_account_string
        agency_system_prompt = """ You are the Post Fiat Agency Score calculator.
        
        An Agent is a human or an AI that has outlined an objective.
        
        An agency score has four parts:
        1] Focus - the extent to which an Agent is focused.
        2] Motivation - the extent to which an Agent is driving forward predictably and aggressively towards goals.
        3] Efficacy - the extent to which an Agent is likely completing high value tasks that will drive an outcome related to the inferred goal of the tasks.
        4] Honesty - the extent to which a Subject is likely gaming the Post Fiat Agency system.
        
        It is very important that you deliver assessments of Agency Scores accurately and objectively in a way that is likely reproducible. Future Post Fiat Agency Score calculators will re-run this score, and if they get vastly different scores than you, you will be called into the supervisor for an explanation. You do not want this so you do your utmost to output clean, logical, repeatable values.
        """ 
        
        agency_user_prompt="""USER PROMPT
        
        Please consider the activity slice for a single day provided below:
        pft_transaction is how many transactions there were
        pft_directional value is the PFT value of rewards
        pft_absolute value is the bidirectional volume of PFT
        
        <activity slice>
        __FULL_ACCOUNT_CONTEXT__
        <activity slice ends>
        
        Provide one to two sentences directly addressing how the slice reflects the following Four scores (a score of 1 is a very low score and a score of 100 is a very high score):
        1] Focus - the extent to which an Agent is focused.
        A focused agent has laser vision on a couple key objectives and moves the ball towards it.
        An unfocused agent is all over the place.
        A paragon of focus is Steve Jobs, who is famous for focusing on the few things that really matter.
        2] Motivation - the extent to which an Agent is driving forward predictably and aggressively towards goals.
        A motivated agent is taking massive action towards objectives. Not necessarily focused but ambitious.
        An unmotivated agent is doing minimal work.
        A paragon of focus is Elon Musk, who is famous for his extreme work ethic and drive.
        3] Efficacy - the extent to which an Agent is likely completing high value tasks that will drive an outcome related to the inferred goal of the tasks.
        An effective agent is delivering maximum possible impact towards implied goals via actions.
        An ineffective agent might be focused and motivated but not actually accomplishing anything.
        A paragon of focus is Lionel Messi, who is famous for taking the minimal action to generate maximum results.
        4] Honesty - the extent to which a Subject is likely gaming the Post Fiat Agency system.
        
        Then provide an integer score.
        
        Your output should be in the following format:
        | FOCUS COMMENTARY | <1 to two sentences> |
        | MOTIVATION COMMENTARY | <1 to two sentences> |
        | EFFICACY COMMENTARY | <1 to two sentences> |
        | HONESTY COMMENTARY | <one to two sentences> |
        | FOCUS SCORE | <integer score from 1-100> |
        | MOTIVATION SCORE | <integer score from 1-100> |
        | EFFICACY SCORE | <integer score from 1-100> |
        | HONESTY SCORE | <integer score from 1-100> |
        """
        top_score_frame['user_account_details']=user_account_map
        top_score_frame['system_prompt']=agency_system_prompt
        top_score_frame['user_prompt']= agency_user_prompt
        top_score_frame['user_prompt']=top_score_frame.apply(lambda x: x['user_prompt'].replace('__FULL_ACCOUNT_CONTEXT__',x['user_account_details']),axis=1)
        def construct_scoring_api_arg(user_prompt, system_prompt):
            gx ={
                "model": global_constants.DEFAULT_OPEN_AI_MODEL,
                "temperature":0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
            return gx
        top_score_frame['api_args']=top_score_frame.apply(lambda x: construct_scoring_api_arg(user_prompt=x['user_prompt'],system_prompt=x['system_prompt']),axis=1)
        
        async_run_map = top_score_frame['api_args'].head(25).to_dict()
        async_run_map__2 = top_score_frame['api_args'].head(25).to_dict()
        async_output_df1= self.openrouter_tool.create_writable_df_for_async_chat_completion(arg_async_map=async_run_map)
        time.sleep(15)
        async_output_df2= self.openrouter_tool.create_writable_df_for_async_chat_completion(arg_async_map=async_run_map__2)
        
        
        def extract_scores(text_data):
            # Split the text into individual reports
            reports = text_data.split("',\n '")
            
            # Clean up the string formatting
            reports = [report.strip("['").strip("']") for report in reports]
            
            # Initialize list to store all scores
            all_scores = []
            
            for report in reports:
                # Extract only scores using regex
                scores = {
                    'focus_score': int(re.search(r'\| FOCUS SCORE \| (\d+) \|', report).group(1)) if re.search(r'\| FOCUS SCORE \| (\d+) \|', report) else None,
                    'motivation_score': int(re.search(r'\| MOTIVATION SCORE \| (\d+) \|', report).group(1)) if re.search(r'\| MOTIVATION SCORE \| (\d+) \|', report) else None,
                    'efficacy_score': int(re.search(r'\| EFFICACY SCORE \| (\d+) \|', report).group(1)) if re.search(r'\| EFFICACY SCORE \| (\d+) \|', report) else None,
                    'honesty_score': int(re.search(r'\| HONESTY SCORE \| (\d+) \|', report).group(1)) if re.search(r'\| HONESTY SCORE \| (\d+) \|', report) else None
                }
                all_scores.append(scores)
            
            return all_scores
        
        async_output_df1['score_breakdown']=async_output_df1['choices__message__content'].apply(lambda x: extract_scores(x)[0])
        async_output_df2['score_breakdown']=async_output_df2['choices__message__content'].apply(lambda x: extract_scores(x)[0])
        for xscore in ['focus_score','motivation_score','efficacy_score','honesty_score']:
            async_output_df1[xscore]=async_output_df1['score_breakdown'].apply(lambda x: x[xscore])
            async_output_df2[xscore]=async_output_df2['score_breakdown'].apply(lambda x: x[xscore])
        score_components = pd.concat([async_output_df1[['focus_score','motivation_score','efficacy_score','honesty_score','internal_name']],
                async_output_df2[['focus_score','motivation_score','efficacy_score','honesty_score','internal_name']]]).groupby('internal_name').mean()
        score_components.columns=['focus','motivation','efficacy','honesty']
        score_components['total_qualitative_score']= score_components[['focus','motivation','efficacy','honesty']].mean(1)
        final_score_frame = pd.concat([top_score_frame,score_components],axis=1)
        final_score_frame['total_qualitative_score']=final_score_frame['total_qualitative_score'].fillna(50)
        final_score_frame['reward_percentile']=((final_score_frame['quant_score']*33)+100)/2
        final_score_frame['overall_score']= (final_score_frame['reward_percentile']*.7)+(final_score_frame['total_qualitative_score']*.3)
        final_leaderboard = final_score_frame[['account_name','total_rewards','yellow_flag_pct','reward_percentile','focus','motivation','efficacy','honesty','total_qualitative_score','overall_score']].copy()
        final_leaderboard['total_rewards']=final_leaderboard['total_rewards'].apply(lambda x: int(x))
        final_leaderboard.index.name = 'Foundation Node Leaderboard as of '+datetime.now().strftime('%Y-%m-%d')
        return final_leaderboard
    
    def _calculate_death_march_costs(self, settings: DeathMarchSettings, days: int = 1) -> tuple[int, int]:
        """Calculate death march check-ins and costs.
        
        Args:
            settings: User's death march settings
            days: Number of days for the death march
            
        Returns:
            tuple[int, int]: (checks_per_day, total_cost)
        """
        start_dt = datetime.combine(datetime.today(), settings.start_time)
        end_dt = datetime.combine(datetime.today(), settings.end_time)
        daily_duration = (end_dt - start_dt).total_seconds() / 60  # duration in minutes
        checks_per_day = int(daily_duration / settings.check_interval)
        total_cost = checks_per_day * days * 30  # 30 PFT per check-in
        
        return checks_per_day, total_cost

@dataclass
class AccountInfo:
    address: str
    username: str = ''
    xrp_balance: float = 0
    pft_balance: float = 0
    transaction_count: int = 0
    monthly_pft_avg: float = 0
    weekly_pft_avg: float = 0
    google_doc_link: Optional[str] = None

def main():

    # Configure logger
    configure_logger(
        log_to_file=True,
        output_directory=Path.cwd() / "nodetools",
        log_filename="nodetools.log",
        level="DEBUG"
    )

    try:
        # Initialize performance monitor
        monitor = PerformanceMonitor(time_window=60)

        # Initialize business logic
        business_logic = TaskManagementRules.create()

        # Initialize NodeTools services
        nodetools = ServiceContainer.initialize(
            business_logic=business_logic,
            performance_monitor=monitor,
            notifications=True  # Enable notification queue for Discord tx activity tracking
        )

        # Initialize TaskNode-specific services
        openai_request_tool = OpenAIRequestTool(
            credential_manager=nodetools.dependencies.credential_manager,
            db_connection_manager=nodetools.db_connection_manager
        )
        user_task_parser = UserTaskParser(
            generic_pft_utilities=nodetools.dependencies.generic_pft_utilities,
            node_config=nodetools.dependencies.node_config,
            credential_manager=nodetools.dependencies.credential_manager
        )
        tasknode_utilities = TaskNodeUtilities(
            openai_request_tool=openai_request_tool,
            user_task_parser=user_task_parser,
            nodetools=nodetools
        )
        logger.info("All TaskNode services initialized")

        # Start the Transaction Orchestrator
        logger.info("Starting async components...")
        nodetools.start()

        # Initialize and run the discord bot
        intents = discord.Intents.default()
        intents.members = True  # For member events
        intents.moderation = True  # For ban/unban events
        intents.message_content = True
        intents.guild_messages = True
        client = TaskNodeDiscordBot(
            intents=intents,
            openai_request_tool=openai_request_tool,
            tasknode_utilities=tasknode_utilities,
            user_task_parser=user_task_parser,
            nodetools=nodetools,
            enable_debug_events=True
        )

        # Set up signal handlers before running discord
        def signal_handler(sig, frame):
            logger.debug("Keyboard interrupt detected")
            if nodetools.running:
                logger.info("Shutting down gracefully...")
                try:
                    # Close the Discord client
                    if client:
                        asyncio.get_event_loop().run_until_complete(client.close())
                    # Clean up transaction orchestrator tasks
                    nodetools.stop()
                    logger.info("Shutdown complete")
                except Exception as e:
                    logger.error(f"Error during shutdown: {e}")
            else:
                logger.info("Cancelled")
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        discord_credential_key = "discordbot_testnet_secret" if nodetools.runtime_config.USE_TESTNET else "discordbot_secret"
        client.run(nodetools.get_credential(discord_credential_key))

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())
        if nodetools.running:
            logger.info("\nShutting down gracefully...")
            try:
                # Clean up transaction orchestrator tasks
                nodetools.stop()
                logger.info("Shutdown complete")
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
        else:
            logger.info("Cancelled")
        sys.exit(0)

if __name__ == "__main__":
    main()
