import discord
from nodetools.models.memo_processor import generate_custom_id
from nodetools.protocols.generic_pft_utilities import GenericPFTUtilities, Response
from decimal import Decimal
from xrpl.wallet import Wallet
from typing import TYPE_CHECKING
from loguru import logger
from imagenode.task_processing.constants import (
    IMAGE_GEN_COST,
    TaskType,
)
import nodetools.configuration.constants as global_constants
import traceback

if TYPE_CHECKING:
    from imagenode.chatbots.pft_image_bot import ImageNodeDiscordBot


class WalletInfoModal(discord.ui.Modal, title="New XRP Wallet"):
    def __init__(
        self, classic_address: str, wallet_seed: str, client: "ImageNodeDiscordBot"
    ):
        super().__init__()
        self.classic_address = classic_address
        self.wallet_seed = wallet_seed
        self.client = client

        self.address = discord.ui.TextInput(
            label="Address (Do not modify)",
            default=self.classic_address,
            style=discord.TextStyle.short,
            required=True,
        )
        self.seed = discord.ui.TextInput(
            label="Secret - Submit Stores. Cancel (Exit)",
            default=self.wallet_seed,
            style=discord.TextStyle.short,
            required=True,
        )
        self.add_item(self.address)
        self.add_item(self.seed)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        logger.debug(
            f"WalletInfoModal.on_submit: Storing seed for user {interaction.user.name} (ID: {user_id})"
        )
        self.client.user_seeds[user_id] = self.seed.value

        # Automatically authorize the address
        await self.client.transaction_repository.authorize_address(
            address=self.address.value,
            auth_source="discord",
            auth_source_user_id=str(user_id),
        )

        await interaction.response.send_message(
            f"Wallet created successfully. You must fund the wallet with {global_constants.MIN_XRP_BALANCE} XRP to use as a Post Fiat Wallet. "
            "The seed is stored to Discord Hot Wallet. To store different seed use /pf_store_seed. ",
            ephemeral=True,
        )


class SeedModal(discord.ui.Modal, title="Store Your Seed"):
    seed = discord.ui.TextInput(label="Seed", style=discord.TextStyle.long)

    def __init__(self, client: "ImageNodeDiscordBot"):
        super().__init__()
        self.client = client  # Save the client reference

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        # Test seed for validity
        try:
            wallet = self.client.generic_pft_utilities.spawn_wallet_from_seed(
                self.seed.value.strip()
            )
        except Exception as e:
            await interaction.response.send_message(
                f"An error occurred while storing your seed: {str(e)}", ephemeral=True
            )
            return

        self.client.user_seeds[user_id] = self.seed.value.strip()  # Store the seed

        # Automatically authorize the address
        await self.client.transaction_repository.authorize_address(
            address=wallet.classic_address,
            auth_source="discord",
            auth_source_user_id=str(user_id),
        )
        await interaction.response.send_message(
            f"Seed stored and address {wallet.classic_address} authorized for user {interaction.user.name}.",
            ephemeral=True,
        )


class PFTImageGenModal(
    discord.ui.Modal, title=f"Generate Image (Uses {IMAGE_GEN_COST} PFT)"
):
    prompt = discord.ui.TextInput(
        label="Prompt", style=discord.TextStyle.long, required=True, max_length=900
    )

    def __init__(self, wallet: Wallet, generic_pft_utilities: GenericPFTUtilities):
        super().__init__(title="Generate Image")
        self.wallet = wallet
        self.generic_pft_utilities = generic_pft_utilities

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Perform the transaction using the details provided in the modal
        destination_address = self.generic_pft_utilities.node_config.node_address
        prompt = self.prompt.value

        try:
            request_id = generate_custom_id()
            response = await self.generic_pft_utilities.send_memo(
                wallet_seed_or_wallet=self.wallet,
                destination=destination_address,
                memo_data=prompt,
                memo_type=request_id + "__" + TaskType.IMAGE_GEN.value,
                pft_amount=Decimal(str(IMAGE_GEN_COST)),
            )

            if not self.generic_pft_utilities.verify_transaction_response(response):
                if isinstance(response, Response):
                    raise Exception(
                        f"Failed to send PFT transaction: {response.result}"
                    )

                raise Exception(f"Failed to send PFT transaction: {response}")

            # extract response from last memo
            tx_info = self.generic_pft_utilities.extract_transaction_info(response)[
                "clean_string"
            ]

            await interaction.followup.send(
                f"Transaction result: {tx_info}", ephemeral=True
            )

        except Exception as e:
            logger.error(f"PFTTransactionModal.on_submit: Error sending memo: {e}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred: {str(e)}", ephemeral=True
            )
            return


# class UpdateLinkModal(discord.ui.Modal, title="Update Google Doc Link"):
#     def __init__(
#         self,
#         seed: str,
#         username: str,
#         client_instance: "ImageNodeDiscordBot",
#         imagenode_utilities: imagenodeUtilities,
#         ephemeral_setting: bool = True,
#     ):
#         super().__init__(title="Update Google Doc Link")
#         self.seed = seed
#         self.username = username
#         self.client: "ImageNodeDiscordBot" = client_instance
#         self.imagenode_utilities = imagenode_utilities
#         self.ephemeral_setting = ephemeral_setting
#
#     google_doc_link = discord.ui.TextInput(
#         label="Please enter new Google Doc Link",
#         style=discord.TextStyle.long,
#         placeholder="Your link will be encrypted but the node operator retains access for effective task generation.",
#     )
#
#     async def on_submit(self, interaction: discord.Interaction):
#         await interaction.response.defer(ephemeral=self.ephemeral_setting)
#
#         try:
#             handshake_success, user_key, node_key, message_obj = (
#                 await self.client._ensure_handshake(
#                     interaction=interaction,
#                     seed=self.seed,
#                     counterparty=self.client.generic_pft_utilities.node_address,
#                     username=self.username,
#                     command_name="pf_update_link",
#                 )
#             )
#
#             if not handshake_success:
#                 return
#
#             await message_obj.edit(
#                 content="Sending encrypted google doc link to node..."
#             )
#
#             await self.imagenode_utilities.discord__update_google_doc_link(
#                 user_seed=self.seed,
#                 google_doc_link=self.google_doc_link.value,
#                 username=self.username,
#             )
#
#             await message_obj.edit(
#                 content=f"Google Doc link updated to {self.google_doc_link.value}"
#             )
#
#         except Exception as e:
#             logger.error(f"UpdateLinkModal.on_submit: Error during update: {str(e)}")
#             await interaction.followup.send(
#                 f"An error occurred during update: {str(e)}",
#                 ephemeral=self.ephemeral_setting,
#             )
