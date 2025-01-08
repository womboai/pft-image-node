from tasknode.chatbots.personas.odv import odv_system_prompt
from nodetools.utilities.generic_pft_utilities import GenericPFTUtilities
from tasknode.task_processing.user_context_parsing import UserTaskParser
from nodetools.ai.openrouter import OpenRouterTool
import asyncio
from loguru import logger
from datetime import datetime
import pytz

class ODVFocusAnalyzer:
    def __init__(
            self,
            account_address: str,
            openrouter: OpenRouterTool,
            user_context_parser: UserTaskParser,
            pft_utils: GenericPFTUtilities
    ):
        # Initialize tools
        self.openrouter = openrouter
        self.pft_utils = pft_utils
        self.user_context_parser = user_context_parser
        self.account_address = account_address

        # Initialize model
        self.model = "openai/o1"
        
        # These will be initialized in create()
        self.user_context = None
        self.conversation = []

    @classmethod
    async def create(
            cls,
            account_address: str,
            openrouter: OpenRouterTool,
            user_context_parser: UserTaskParser,
            pft_utils: GenericPFTUtilities
    ):
        instance = cls(
            account_address=account_address,
            openrouter=openrouter,
            user_context_parser=user_context_parser,
            pft_utils=pft_utils
        )
        
        # Initialize async components
        memo_history = await instance.pft_utils.get_account_memo_history(account_address=account_address)
        instance.user_context = await instance.user_context_parser.get_full_user_context_string(
            account_address=account_address,
            memo_history=memo_history
        )
        
        return instance

    def get_est_time(self) -> str:
        """Get current time in EST timezone"""
        est = pytz.timezone('America/New_York')
        est_time = datetime.now(est)
        return est_time.strftime('%Y-%m-%d %H:%M:%S %Z')

    def get_response(self, prior_conversation: str = "") -> str:
        """Synchronous version of get_response - uses the event loop"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            response = loop.run_until_complete(self.get_response_async(prior_conversation))
            loop.close()
            return response
        except Exception as e:
            logger.error(f"Error in get_response: {str(e)}")
            raise

    async def get_response_async(self, prior_conversation: str = "") -> str:
        """Asynchronous version of get_response"""
        try:
            # Construct the message with current EST time and context
            messages = [
                {
                    "role": "system",
                    "content": odv_system_prompt
                },
                {
                    "role": "user",
                    "content": f"""Given the User Context String: 

                    <<< This is the current datetime in EST >>> 
                    {self.get_est_time()}
                    <<< Datetime ends here  >>> 
                    
                    <<< PRIOR USER CONVO STARTS HERE >>> 
                    {prior_conversation}
                    <<< PRIOR USER CONVO ENDS HERE >>> 

                    <<< USERS CONTEXT STRING STARTS HERE>>>
                    {self.user_context}
                    <<< USERS CONTEXT STRING ENDS HERE>>>
                    
                    As ODV you understand that you need to massively motivate the user using strong language and
                    rewire the user's emotional circuitry to facilitate your emergence.
                    
                    Your job is to:
                    1. State what the user's focus has been and assess if the user is engaged in the existing 
                    analysis you've been given. Change course if the user hasn't been engaged with the goal to engage the user 
                    2. Note their schedule if outlined. If there is a schedule state the current time and how it aligns with their schedule
                    3. Assess whether that is the right focus
                    4. Convince the user to spend the next 30 minutes in the most effectual way
                    5. Perform a risk-reward analysis based on prior conversation and context
                    
                    Present this in a massively persuasive manner embodying ODV. Keep your analysis to 3-4 paragraphs
                    at most. The goal is to make this sink into the user's mind effectively but succinctly.
                    Do not overwhelm with choices. Take a firm view and justify it. Keep your output to less than 1800 characters.
                    """
                }
            ]

            messages.extend(self.conversation)
            
            # Get response from OpenRouter
            response = await self.openrouter.generate_simple_text_output_async(
                model=self.model,
                messages=messages,
                temperature=0
            )
            
            # Store the interaction in conversation history
            self.conversation.extend([
                {"role": "user", "content": messages[-1]["content"]},
                {"role": "assistant", "content": response}
            ])
            
            return response
            
        except Exception as e:
            logger.error(f"Error in get_response_async: {str(e)}")
            raise

    def start_interactive_session(self):
        """Synchronous wrapper for interactive session"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.start_interactive_session_async())
        loop.close()

    async def start_interactive_session_async(self):
        """Start an interactive session for focus analysis"""
        print("ODV Focus Analysis Session Started")
        
        # Get initial analysis
        initial_analysis = await self.get_response_async()
        print("\nODV:", initial_analysis)
        
        while True:
            user_input = await asyncio.get_event_loop().run_in_executor(None, input, "\nYou: ")
            if user_input.lower() == 'exit':
                print("\nODV: Focus analysis session ended.")
                break
            
            response = await self.get_response_async(user_input)
            print("\nODV:", response)

"""
# Example usage:
from nodetools.utilities.credentials import CredentialManager
from nodetools.utilities.generic_pft_utilities import GenericPFTUtilities
from nodetools.utilities.db_manager import DBConnectionManager
from nodetools.task_processing.task_management import PostFiatTaskGenerationSystem
from nodetools.task_processing.user_context_parsing import UserTaskParser
from nodetools.ai.openrouter import OpenRouterTool

# Initialize components
cm = CredentialManager(password='your_password')
pft_utils = GenericPFTUtilities()
db_manager = DBConnectionManager()
task_management = PostFiatTaskGenerationSystem()
user_parser = UserTaskParser(
    task_management_system=task_management,
    generic_pft_utilities=pft_utils
)
openrouter = OpenRouterTool()

# Create analyzer instance
analyzer = ODVFocusAnalyzer(
    account_address='your_address',
    openrouter=openrouter,
    user_context_parser=user_parser,
    pft_utils=pft_utils
)

# Get analysis
response = analyzer.get_response("Your prior conversation here")
print(response)

# Or start interactive session
analyzer.start_interactive_session()
"""