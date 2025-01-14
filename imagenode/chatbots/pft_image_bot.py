# standard imports
import asyncio
from pathlib import Path
from dataclasses import dataclass
import traceback
import sys
from typing import Optional, Dict, Any
import signal

# third party imports
import discord
from discord import Object, Interaction, app_commands
from loguru import logger

# nodetools imports
import nodetools.configuration.constants as global_constants

from nodetools.configuration.configure_logger import configure_logger
from nodetools.performance.monitor import PerformanceMonitor
from nodetools.container.service_container import ServiceContainer
from nodetools.protocols.generic_pft_utilities import Wallet

# imagenode imports
from imagenode.task_processing.constants import (
    DISCORD_SUPER_USER_IDS,
    IMAGE_GEN_COST,
    TaskType,
)
from imagenode.task_processing.core_business_logic import ImageGenerationRules
from imagenode.chatbots.discord_modals import (
    SeedModal,
    PFTImageGenModal,
    WalletInfoModal,
)


@dataclass
class AccountInfo:
    address: str
    username: str = ""
    xrp_balance: float = 0
    pft_balance: float = 0
    transaction_count: int = 0
    monthly_pft_avg: float = 0
    weekly_pft_avg: float = 0
    google_doc_link: Optional[str] = None


class ImageNodeDiscordBot(discord.Client):

    NON_EPHEMERAL_USERS = {427471329365590017}

    def __init__(self, *args, nodetools: ServiceContainer, **kwargs):
        super().__init__(*args, **kwargs)
        # Get network configuration and set network-specific attributes
        self.network_config = nodetools.network_config
        self.node_config = nodetools.node_config

        # Initialize components
        self.generic_pft_utilities = nodetools.dependencies.generic_pft_utilities
        self.transaction_repository = nodetools.dependencies.transaction_repository

        self.user_seeds = {}
        self.tree = app_commands.CommandTree(self)
        self.cache_timeout = 300  # seconds
        self.notification_queue: asyncio.Queue = nodetools.notification_queue

    # User is long lasting
    def is_special_user_non_ephemeral(self, interaction: discord.Interaction) -> bool:
        """Return False if the user is not in the NON_EPHEMERAL_USERS set, else True."""
        output = not (interaction.user.id in self.NON_EPHEMERAL_USERS)
        return output

    async def setup_hook(self):
        """Sets up the slash commands for the bot and initiates background tasks."""
        guild_id = self.node_config.discord_guild_id
        guild = Object(id=guild_id)

        self.bg_task = self.loop.create_task(
            self.transaction_notifier(), name="DiscordBotTransactionNotifier"
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
            logger.info(
                f"Member remove event received for user {user.name} (ID: {user.id}). Deauthorizing addresses..."
            )
            try:
                # Remove their seed if it exists
                self.user_seeds.pop(user.id, None)

                # Deauthorize all addresses associated with this Discord user
                await self.transaction_repository.deauthorize_addresses(
                    auth_source="discord", auth_source_user_id=str(user.id)
                )

            except Exception as e:
                logger.error(
                    f"Error deauthorizing addresses for banned user {user.name} (ID: {user.id}): {e}"
                )
                logger.error(traceback.format_exc())

        @self.tree.command(
            name="pf_new_wallet", description="Generate a new XRP wallet", guild=guild
        )
        async def pf_new_wallet(interaction: Interaction):
            # Generate the wallet
            new_wallet = Wallet.create()

            # Create the modal with the client reference and send it
            modal = WalletInfoModal(
                classic_address=new_wallet.classic_address,
                wallet_seed=new_wallet.seed,
                client=interaction.client,
            )
            await interaction.response.send_modal(modal)

        # @self.tree.command(
        #     name="pf_show_seed", description="Show your stored seed", guild=guild
        # )
        # async def pf_show_seed(interaction: discord.Interaction):
        #     user_id = interaction.user.id
        #
        #     # Check if the user has a stored seed
        #     if user_id in self.user_seeds:
        #         seed = self.user_seeds[user_id]
        #
        #         # Create and send an ephemeral message with the seed
        #         await interaction.response.send_message(
        #             f"Your stored seed is: {seed}\n"
        #             "This message will be deleted in 30 seconds for security reasons.",
        #             ephemeral=True,
        #             delete_after=30,
        #         )
        #     else:
        #         await interaction.response.send_message(
        #             "No seed found for your account. Use /pf_store_seed to store a seed first.",
        #             ephemeral=True,
        #         )

        @self.tree.command(
            name="pf_guide",
            description="Show a guide of all available commands",
            guild=guild,
        )
        async def pf_guide(interaction: discord.Interaction):
            guide_text = f"""
# Post Fiat Discord Bot Guide

### Info Commands
1. /pf_guide: Show this guide
2. /pf_my_wallet: Show information about your stored wallet.
3. /wallet_info: Get information about a specific wallet address.

### Wallet Initialization
1. /pf_new_wallet: Generate a new XRP wallet. You need to fund via Coinbase etc to continue
2. /pf_store_seed: Stores wallet seeds for transactions through this bot.

### Image Generation 
1. /pf_gen_image: Open a form to generate an image using {IMAGE_GEN_COST} PFT.

Note: XRP wallets need {global_constants.MIN_XRP_BALANCE} XRP to transact.
We recommend funding with a bit more to cover ongoing transaction fees.
"""

            await interaction.response.send_message(guide_text, ephemeral=True)

        @self.tree.command(
            name="pf_my_wallet", description="Show your wallet information", guild=guild
        )
        async def pf_my_wallet(interaction: discord.Interaction):
            user_id = interaction.user.id
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)

            # Defer the response to avoid timeout
            await interaction.response.defer(ephemeral=ephemeral_setting)

            if user_id not in self.user_seeds:
                await interaction.followup.send(
                    "No seed found for your account. Use /pf_store_seed to store a seed first.",
                    ephemeral=True,
                )
                return

            try:
                seed = self.user_seeds[user_id]
                logger.debug(
                    f"ImageNodeDiscordBot.setup_hook.pf_my_wallet: Spawning wallet to fetch info for {interaction.user.name}"
                )
                wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed)
                wallet_address = wallet.classic_address

                # Get account info
                account_info = await self.generate_basic_balance_info_string(
                    address=wallet.address
                )

                # Get recent messages
                incoming_messages, outgoing_messages = (
                    await self.generic_pft_utilities.get_recent_messages(wallet_address)
                )

                # Split long strings if they exceed Discord's limit
                def truncate_field(content, max_length=1024):
                    if len(content) > max_length:
                        return content[: max_length - 3] + "..."
                    return content

                # Create multiple embeds if needed
                embeds = []

                # First embed with basic info
                embed = discord.Embed(title="Your Wallet Information", color=0x00FF00)
                embed.add_field(
                    name="Wallet Address", value=wallet_address, inline=False
                )

                # Split account info into multiple fields if needed
                if len(account_info) > 1024:
                    parts = [
                        account_info[i : i + 1024]
                        for i in range(0, len(account_info), 1024)
                    ]
                    for i, part in enumerate(parts):
                        embed.add_field(
                            name=f"Balance Information {i+1}", value=part, inline=False
                        )
                else:
                    embed.add_field(
                        name="Balance Information", value=account_info, inline=False
                    )

                embeds.append(embed)

                if incoming_messages or outgoing_messages:
                    embed2 = discord.Embed(title="Recent Transactions", color=0x00FF00)

                    if incoming_messages:
                        incoming = truncate_field(incoming_messages)
                        embed2.add_field(
                            name="Most Recent Incoming Transaction",
                            value=incoming,
                            inline=False,
                        )

                    if outgoing_messages:
                        outgoing = truncate_field(outgoing_messages)
                        embed2.add_field(
                            name="Most Recent Outgoing Transaction",
                            value=outgoing,
                            inline=False,
                        )

                    embeds.append(embed2)

                # Send all embeds
                await interaction.followup.send(
                    embeds=embeds, ephemeral=ephemeral_setting
                )

            except Exception as e:
                error_message = f"An unexpected error occurred: {str(e)}. Please try again later or contact support if the issue persists."
                logger.error(
                    f"ImageNodeDiscordBot.pf_my_wallet: An error occurred: {str(e)}"
                )
                logger.error(traceback.format_exc())
                await interaction.followup.send(error_message, ephemeral=True)

        @self.tree.command(
            name="wallet_info",
            description="Get information about a wallet",
            guild=guild,
        )
        async def wallet_info(interaction: discord.Interaction, wallet_address: str):
            ephemeral_setting = self.is_special_user_non_ephemeral(interaction)
            try:
                account_info = await self.generate_basic_balance_info_string(
                    address=wallet_address, owns_wallet=False
                )

                # Create an embed for better formatting
                embed = discord.Embed(title="Wallet Information", color=0x00FF00)
                embed.add_field(
                    name="Wallet Address", value=wallet_address, inline=False
                )
                embed.add_field(name="Account Info", value=account_info, inline=False)

                await interaction.response.send_message(
                    embed=embed, ephemeral=ephemeral_setting
                )
            except Exception as e:
                logger.error(
                    f"ImageNodeDiscordBot.wallet_info: An error occurred: {str(e)}"
                )
                logger.error(traceback.format_exc())
                await interaction.response.send_message(
                    f"An error occurred: {str(e)}", ephemeral=True
                )

        @self.tree.command(
            name="admin_change_ephemeral_setting",
            description="Change the ephemeral setting for self",
            guild=guild,
        )
        async def admin_change_ephemeral_setting(
            interaction: discord.Interaction, public: bool
        ):
            # Check if the user has permission (matches the specific ID)
            if interaction.user.id not in DISCORD_SUPER_USER_IDS:
                await interaction.response.send_message(
                    "You don't have permission to use this command.", ephemeral=True
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
                f"Your messages will now be {setting}", ephemeral=True
            )

        @self.tree.command(
            name="pf_gen_image",
            description=f"Open form to generate an image (Requires {IMAGE_GEN_COST} PFT)",
            guild=guild,
        )
        async def pf_gen_image(interaction: Interaction):
            user_id = interaction.user.id

            # Check if the user has a stored seed
            if user_id not in self.user_seeds:
                await interaction.response.send_message(
                    "You must store a seed using /store_seed before generating image.",
                    ephemeral=True,
                )
                return

            seed = self.user_seeds[user_id]
            wallet = self.generic_pft_utilities.spawn_wallet_from_seed(seed=seed)

            try:
                pft_balance = await self.generic_pft_utilities.fetch_pft_balance(
                    wallet.address
                )

                if pft_balance < IMAGE_GEN_COST:
                    await interaction.response.send_message(
                        f"Insufficient PFT to generate an image. At least {IMAGE_GEN_COST} PFT is required.",
                        ephemeral=True,
                    )
                    return
            except Exception as e:
                logger.error(
                    f"Error fetching pft_balance for wallet with address {wallet.address}: {e}"
                )
                await interaction.response.send_message(
                    f"Issue retrieving PFT balance. Ensure you have at least {IMAGE_GEN_COST} PFT in your wallet and try again.",
                    ephemeral=True,
                )
                return
            # Pass the user's wallet to the modal
            await interaction.response.send_modal(
                PFTImageGenModal(
                    wallet=wallet, generic_pft_utilities=self.generic_pft_utilities
                )
            )

        @self.tree.command(
            name="pf_store_seed", description="Store a seed", guild=guild
        )
        async def store_seed(interaction: discord.Interaction):
            await interaction.response.send_modal(SeedModal(client=self))
            logger.debug(
                f"ImageNodeDiscordBot.store_seed: Seed storage command executed by {interaction.user.name}"
            )

        await self.tree.sync(guild=guild)
        logger.debug(f"ImageNodeDiscordBot.setup_hook: Slash commands synced")

        commands = await self.tree.fetch_commands(guild=guild)
        logger.debug(f"Registered commands: {[cmd.name for cmd in commands]}")

    async def on_ready(self):
        logger.debug(
            f"ImageNodeDiscordBot.on_ready: Logged in as {self.user} (ID: {self.user.id})"
        )
        logger.debug("ImageNodeDiscordBot.on_ready: ------------------------------")
        logger.debug("ImageNodeDiscordBot.on_ready: Connected to the following guilds:")
        for guild in self.guilds:
            logger.debug(f"- {guild.name} (ID: {guild.id})")

    async def transaction_notifier(self):
        await self.wait_until_ready()
        channel = self.get_channel(self.node_config.discord_activity_channel_id)

        if not channel:
            logger.error(
                f"ImageNodeDiscordBot.transaction_notifier: Channel with ID "
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

    def parse_possible_image(self, tx: Dict[str, Any]) -> str:
        hash: str | None = None
        # NOTE: this is highly dependent on the IMAGE RESPONSE format
        if TaskType.IMAGE_GEN_RESPONSE.value in tx["memo_type"]:
            hash = tx["memo_data"].split("ipfs hash: ")[1].strip("`")

        image_string = (
            "" if hash is None else f"Image: https://gateway.pinata.cloud/ipfs/{hash}"
        )

        return image_string

    def format_notification(self, tx: Dict[str, Any]) -> str:
        """Format the reviewing result for Discord"""

        url = self.network_config.explorer_tx_url_mask.format(hash=tx["hash"])

        return (
            f"Date: {tx['datetime']}\n"
            f"Account: `{tx['account']}`\n"
            f"Memo Format: `{tx['memo_format']}`\n"
            f"Memo Type: `{tx['memo_type']}`\n"
            f"Memo Data: `{tx['memo_data']}`\n"
            f"PFT: {tx.get('pft_absolute_amount', 0)}\n"
            f"URL: {url}\n"
            f"{self.parse_possible_image(tx)}"
        )

    async def generate_basic_balance_info_string(
        self, address: str, owns_wallet: bool = True
    ) -> str:
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
            account_info.xrp_balance = (
                await self.generic_pft_utilities.fetch_xrp_balance(address)
            )
            account_info.pft_balance = (
                await self.generic_pft_utilities.fetch_pft_balance(address)
            )
        except Exception as e:
            # Account probably not activated yet
            account_info.xrp_balance = 0
            account_info.pft_balance = 0

        try:
            memo_history = await self.generic_pft_utilities.get_account_memo_history(
                account_address=address
            )

            if not memo_history.empty:

                # transaction count
                account_info.transaction_count = len(memo_history)

                # Likely username
                outgoing_memo_format = list(
                    memo_history[memo_history["direction"] == "OUTGOING"][
                        "memo_format"
                    ].mode()
                )
                if len(outgoing_memo_format) > 0:
                    account_info.username = outgoing_memo_format[0]
                else:
                    account_info.username = "Unknown"

            # Get google doc link
            # if owns_wallet:
            #     account_info.google_doc_link = await self.user_task_parser.get_latest_outgoing_context_doc_link(address)

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
                    NUM PFT MEMO TX:  {info.transaction_count}"""

        if info.google_doc_link:
            output += f"\n\nCONTEXT DOC:      {info.google_doc_link}"

        return output


def main():

    # Configure logger
    configure_logger(
        log_to_file=True,
        output_directory=Path.cwd() / "nodetools",
        log_filename="nodetools.log",
        level="DEBUG",
    )

    try:
        # Initialize performance monitor
        monitor = PerformanceMonitor(time_window=60)

        # Initialize business logic
        business_logic = ImageGenerationRules.create()

        # Initialize NodeTools services
        nodetools = ServiceContainer.initialize(
            business_logic=business_logic,
            performance_monitor=monitor,
            notifications=True,  # Enable notification queue for Discord tx activity tracking
        )

        # Start the Transaction Orchestrator
        logger.info("Starting async components...")
        nodetools.start()

        # Initialize and run the discord bot
        intents = discord.Intents.default()
        intents.members = True  # For member events
        intents.moderation = True  # For ban/unban events
        intents.message_content = True
        intents.guild_messages = True
        client = ImageNodeDiscordBot(
            intents=intents, nodetools=nodetools, enable_debug_events=True
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

        discord_credential_key = (
            "discordbot_testnet_secret"
            if nodetools.runtime_config.USE_TESTNET
            else "discordbot_secret"
        )
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
