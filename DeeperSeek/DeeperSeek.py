from os import environ
from time import time
from typing import Optional
from platform import system
from logging import DEBUG, Formatter, StreamHandler, getLogger
from asyncio import sleep, get_event_loop
from re import match
from bs4 import BeautifulSoup

from inscriptis import get_text

import zendriver

from .internal.objects import Response, SearchResult, Theme
from .internal.selectors import DeepSeekSelectors
from .internal.exceptions import (MissingCredentials, InvalidCredentials, ServerDown, MissingInitialization, CouldNotFindElement,
                                  InvalidChatID)

class DeepSeek:
    def __init__(
        self,
        token: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        chat_id: Optional[str] = None,
        headless: bool = True,
        verbose: bool = False,
        chrome_args: list = [],
        attempt_cf_bypass: bool = True
    ) -> None:
        """Initializes the DeepSeek object.

        Args
        ---------
        token: Optional[str]
            The token of the user.
        email: Optional[str]
            The email of the user.
        password: Optional[str]
            The password of the user.
        chat_id: str
            The chat id.
        headless: bool
            Whether to run the browser in headless mode.
        verbose: bool
            Whether to log the actions.
        chrome_args: list
            The arguments to pass to the Chrome browser.
        attempt_cf_bypass: bool
            Whether to attempt to bypass the Cloudflare protection.

        Raises
        ---------
        ValueError:
            Either the token or the email and password must be provided.
        """
        if not token and not (email and password):
            raise MissingCredentials("Either the token alone or the email and password both must be provided")

        self._email = email
        self._password = password
        self._token = token
        self._chat_id = chat_id
        self._headless = headless
        self._verbose = verbose
        self._chrome_args = chrome_args
        self._attempt_cf_bypass = attempt_cf_bypass

        self._deepthink_enabled = False
        self._search_enabled = False
        self._initialized = False
        self.selectors = DeepSeekSelectors()

    async def initialize(self) -> None:
        """Initializes the DeepSeek session.

        This method sets up the logger, starts a virtual display if necessary, and launches the browser.
        It also navigates to the DeepSeek chat page and handles the login process using either a token
        or email and password.

        Raises
        ---------
        ValueError:
            PyVirtualDisplay is not installed.
        ValueError:
            Xvfb is not installed.
        """

        # Initilize the logger
        self.logger = getLogger("DeeperSeek")
        self.logger.setLevel(DEBUG)

        if self._verbose:
            handler = StreamHandler()
            handler.setFormatter(Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
            self.logger.addHandler(handler)

        # Start the virtual display if the system is Linux and the DISPLAY environment variable is not set
        if system() == "Linux" and "DISPLAY" not in environ:
            self.logger.debug("Starting virtual display...")
            try:
                from pyvirtualdisplay.display import Display

                self.display = Display()
                self.display.start()
            except ModuleNotFoundError:
                raise ValueError(
                    "Please install PyVirtualDisplay to start a virtual display by running `pip install PyVirtualDisplay`"
                )
            except FileNotFoundError as e:
                if "No such file or directory: 'Xvfb'" in str(e):
                    raise ValueError(
                        "Please install Xvfb to start a virtual display by running `sudo apt install xvfb`"
                    )
                raise e

        # Start the browser
        self.browser = await zendriver.start(
            chrome_args = self._chrome_args,
            headless = self._headless
        )

        self.logger.debug("Navigating to the chat page...")
        await self.browser.get("https://chat.deepseek.com/" if not self._chat_id \
            else f"https://chat.deepseek.com/a/chat/s/{self._chat_id}")

        if self._attempt_cf_bypass:
            try:
                self.logger.debug("Verifying the Cloudflare protection...")
                await self.browser.main_tab.verify_cf()
            except: # It times out if there was no need to verify
                pass
        
        self._initialized = True
        self._is_active = True
        loop = get_event_loop()
        loop.create_task(self._keep_alive())
        
        if self._token:
            await self._login()
        else:
            await self._login_classic()
    
    async def _keep_alive(self) -> None:
        """Keeps the browser alive by refreshing the page periodically."""
        try:
            while self._is_active:
                await sleep(300)  # Sleep for 5 minutes (adjustable)
                if hasattr(self, "browser"):
                    # self.logger.debug("Refreshing the page to keep session alive...")
                    # await self.browser.main_tab.reload()
                    continue
        except Exception as e:
            self.logger.error(f"Keep-alive encountered an error: {e}")

    def __del__(self) -> None:
        """Destructor method to stop the browser and the virtual display."""

        self._is_active = False

    async def _login(self) -> None:
        """Logs in to DeepSeek using a token.

        This method sets the token in the browser's local storage and reloads the page to authenticate.
        If the token is invalid, it falls back to the classic login method. (email and password)

        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        self.logger.debug("Logging in using the token...")
        await self.browser.main_tab.evaluate(
            f"localStorage.setItem('userToken', JSON.stringify({{value: '{self._token}', __version: '0'}}))",
            await_promise = True,
            return_by_value = True
        )
        await self.browser.main_tab.reload()
        
        # Reloading with an invalid token still gives access to the website somehow, but only for a split second
        # So I added a delay to make sure the token is actually invalid
        await sleep(2)
        
        # Check if the token login was successful
        try:
            await self.browser.main_tab.wait_for(self.selectors.interactions.textbox, timeout = 5)
        except:
            self.logger.debug("Token failed, logging in using email and password...")

            if self._email and self._password:
                return await self._login_classic(token_failed = True)
            else:
                raise InvalidCredentials("The token is invalid and no email or password was provided")
    
        self.logger.debug("Token login successful!")
        
    async def _login_classic(self, token_failed: bool = False) -> None:
        """Logs in to DeepSeek using email and password."""
        
        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")
        
        self.logger.debug("Attempting to login with email and password...")
            
        # 1. Wait longer for the page to fully load
        try:
            await sleep(5)  # Increased initial wait time
            await self.browser.main_tab.evaluate(
                "document.readyState === 'complete'",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug("Page loading complete")
        except Exception as e:
            self.logger.error(f"Page loading check failed: {str(e)}")
            
        # 2. Take a screenshot for debugging if possible
        try:
            self.logger.debug("Capturing page structure for debugging...")
            current_url = await self.browser.main_tab.evaluate(
                "window.location.href",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug(f"Current URL: {current_url}")
            
            page_source = await self.browser.main_tab.evaluate(
                "document.documentElement.outerHTML",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug(f"Page HTML size: {len(page_source)} bytes")
        except Exception as e:
            self.logger.error(f"Failed to capture debug info: {str(e)}")
        
        # 3. Try to detect if we need to navigate to login first
        try:
            # Check if we need to navigate to login page first
            has_login_button = await self.browser.main_tab.evaluate(
                """
                (function() {
                    // Look for "Log in" or "Sign in" buttons that might need to be clicked first
                    const loginLinks = Array.from(document.querySelectorAll('a, button, div[role="button"]'))
                        .filter(el => {
                            const text = el.textContent.toLowerCase();
                            return text.includes('log in') || 
                                   text.includes('sign in') || 
                                   text.includes('login') ||
                                   text.includes('signin');
                        });
                    
                    if (loginLinks.length > 0) {
                        loginLinks[0].click();
                        return true;
                    }
                    return false;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if has_login_button:
                self.logger.debug("Clicked on a login button to access login form")
                await sleep(3)  # Wait for navigation or form to appear
        except Exception as e:
            self.logger.error(f"Error checking for login navigation: {str(e)}")
        
        # 4. Use completely JS-based approach to find and fill the login form
        login_successful = await self.browser.main_tab.evaluate(
            f"""
            (function() {{
                console.log("Starting JS-based login");
                
                // Wait a moment to ensure any animations are complete
                setTimeout(() => {{}}, 1000);
                
                // First find all input fields
                const allInputs = document.querySelectorAll('input');
                console.log("Found " + allInputs.length + " input fields");
                
                // Look for email/username field using various attributes
                let emailInput = null;
                let passwordInput = null;
                
                for (const input of allInputs) {{
                    const type = input.type?.toLowerCase() || '';
                    const name = input.name?.toLowerCase() || '';
                    const placeholder = input.placeholder?.toLowerCase() || '';
                    const id = input.id?.toLowerCase() || '';
                    const ariaLabel = input.getAttribute('aria-label')?.toLowerCase() || '';
                    
                    // Check for email/text input
                    if ((type === 'email' || type === 'text') && 
                        (name.includes('email') || name.includes('username') || 
                         placeholder.includes('email') || placeholder.includes('username') ||
                         id.includes('email') || id.includes('username') || 
                         ariaLabel.includes('email') || ariaLabel.includes('username'))) {{
                        emailInput = input;
                        console.log("Found email input", input);
                    }}
                    
                    // Check for password input
                    if (type === 'password') {{
                        passwordInput = input;
                        console.log("Found password input", input);
                    }}
                }}
                
                // If we didn't find them by specific attributes, just try the first text and password inputs
                if (!emailInput) {{
                    emailInput = document.querySelector('input[type="text"], input[type="email"]');
                    console.log("Falling back to first text input", emailInput);
                }}
                
                if (!passwordInput) {{
                    passwordInput = document.querySelector('input[type="password"]');
                    console.log("Falling back to first password input", passwordInput);
                }}
                
                // If we still don't have both inputs, we can't continue
                if (!emailInput || !passwordInput) {{
                    console.log("Could not find login form inputs");
                    return false;
                }}
                
                // Fill in the email and password
                try {{
                    // Focus and clear the field first
                    emailInput.focus();
                    emailInput.value = '';
                    emailInput.value = "{self._email}";
                    
                    // Dispatch events to ensure the UI updates
                    emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    emailInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    
                    console.log("Filled email input");
                    
                    // Now do the same for password
                    passwordInput.focus();
                    passwordInput.value = '';
                    passwordInput.value = "{self._password}";
                    
                    passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    
                    console.log("Filled password input");
                }} catch (e) {{
                    console.error("Error filling form:", e);
                    return false;
                }}
                
                // Now find and click any checkboxes (for terms agreement, etc.)
                try {{
                    const checkboxes = document.querySelectorAll('input[type="checkbox"], div[role="checkbox"]');
                    for (const checkbox of checkboxes) {{
                        checkbox.click();
                        console.log("Clicked checkbox");
                    }}
                }} catch (e) {{
                    console.error("Error clicking checkboxes:", e);
                    // Continue anyway as checkboxes might not be required
                }}
                
                // Now find and click the login/submit button
                try {{
                    // Look for login button by various attributes
                    let loginButton = null;
                    
                    // First by type="submit"
                    const submitButtons = document.querySelectorAll('button[type="submit"], input[type="submit"]');
                    if (submitButtons.length > 0) {{
                        loginButton = submitButtons[0];
                    }}
                    
                    // Then by text content
                    if (!loginButton) {{
                        const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                        loginButton = buttons.find(el => {{
                            const text = el.textContent.toLowerCase();
                            return text.includes('log in') || 
                                   text.includes('sign in') || 
                                   text.includes('login') || 
                                   text.includes('submit') ||
                                   text === 'continue';
                        }});
                    }}
                    
                    // If still not found, use any button near the password field
                    if (!loginButton && passwordInput) {{
                        const rect = passwordInput.getBoundingClientRect();
                        const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                        
                        // Sort by proximity to password field
                        const sortedButtons = buttons.sort((a, b) => {{
                            const aRect = a.getBoundingClientRect();
                            const bRect = b.getBoundingClientRect();
                            
                            // Calculate vertical distance (give preference to buttons below the password field)
                            const aVertDist = aRect.top >= rect.bottom ? 
                                             aRect.top - rect.bottom : 
                                             1000 + (rect.top - aRect.bottom);
                            
                            const bVertDist = bRect.top >= rect.bottom ? 
                                             bRect.top - rect.bottom : 
                                             1000 + (rect.top - bRect.bottom);
                            
                            return aVertDist - bVertDist;
                        }});
                        
                        if (sortedButtons.length > 0) {{
                            loginButton = sortedButtons[0];
                        }}
                    }}
                    
                    if (loginButton) {{
                        console.log("Found login button, clicking it");
                        loginButton.click();
                        console.log("Clicked login button");
                        return true;
                    }} else {{
                        console.log("Could not find login button");
                        return false;
                    }}
                }} catch (e) {{
                    console.error("Error clicking login button:", e);
                    return false;
                }}
            }})()
            """,
            await_promise=True,
            return_by_value=True
        )
        
        if not login_successful:
            self.logger.error("JavaScript login approach failed")
            raise InvalidCredentials("Could not find or interact with login form elements")
        
        # 5. Wait for successful login with increased patience
        self.logger.debug("Waiting for login to complete...")
        try:
            # Try several selectors that might indicate successful login
            await sleep(10)  # Increased wait time after login button click
            
            # Check if URL indicates success (redirected from login page)
            current_url = await self.browser.main_tab.evaluate(
                "window.location.href",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug(f"Current URL after login attempt: {current_url}")
            
            # Use comprehensive checks for successful login
            login_successful = await self.browser.main_tab.evaluate(
                """
                (function() {
                    // Check for ANY of these indicators of successful login
                    
                    // 1. URL indicates success - we're out of the login page
                    const url = window.location.href.toLowerCase();
                    if (url.includes('/chat') || !url.includes('/login') && 
                        !url.includes('/signin') && !url.includes('/sign-in')) {
                        console.log("Login seems successful based on URL");
                        return true;
                    }
                    
                    // 2. UI elements that indicate successful login
                    const userElements = document.querySelectorAll(
                        'textarea, div[contenteditable="true"], ' +
                        'div[class*="avatar"], div[class*="profile"], ' +
                        'div[class*="chat"], div[class*="message"]'
                    );
                    
                    if (userElements.length > 0) {
                        console.log("Found user/chat interface elements");
                        return true;
                    }
                    
                    // 3. Login form is gone
                    if (!document.querySelector('input[type="password"]')) {
                        console.log("Password field is gone, likely logged in");
                        return true;
                    }
                    
                    // 4. Check for error messages
                    const errorMessages = Array.from(document.querySelectorAll('div, p, span'))
                        .filter(el => {
                            const text = el.textContent.toLowerCase();
                            return text.includes('invalid') || 
                                   text.includes('incorrect') || 
                                   text.includes('failed') || 
                                   text.includes('wrong');
                        });
                    
                    if (errorMessages.length > 0) {
                        console.log("Found error messages:", errorMessages[0].textContent);
                        return false;
                    }
                    
                    console.log("No clear indicators of login success or failure");
                    return false;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if login_successful:
                self.logger.debug("Login appears successful!")
                
                # If we're on a welcome page, navigate to chat
                if not await self.find_textbox():
                    self.logger.debug("No chatbox found, attempting to navigate to main chat interface")
                    
                    # Navigate to chat page directly
                    await self.browser.main_tab.get("https://chat.deepseek.com/")
                    await sleep(5)  # Wait for navigation
                    
                    # Check if we now have a textbox
                    if await self.find_textbox():
                        self.logger.debug("Successfully navigated to chat interface")
                    else:
                        self.logger.debug("Navigation to chat interface didn't show textbox, but login appears successful")
            else:
                self.logger.error("Login unsuccessful - couldn't detect success indicators")
                
                # Check specifically for credential errors
                credential_error = await self.browser.main_tab.evaluate(
                    """
                    (function() {
                        const errorMessages = Array.from(document.querySelectorAll('div, p, span'))
                            .filter(el => {
                                const text = el.textContent.toLowerCase();
                                return text.includes('password') || 
                                       text.includes('email') || 
                                       text.includes('account');
                            })
                            .map(el => el.textContent.trim())
                            .filter(text => 
                                text.includes('invalid') || 
                                text.includes('incorrect') || 
                                text.includes('wrong') ||
                                text.includes('failed')
                            );
                        
                        return errorMessages.length > 0 ? errorMessages[0] : null;
                    })()
                    """,
                    await_promise=True,
                    return_by_value=True
                )
                
                if credential_error:
                    raise InvalidCredentials(f"Login error: {credential_error}")
                else:
                    # Try forced navigation as last resort
                    self.logger.debug("Attempting forced navigation to chat as last resort")
                    await self.browser.main_tab.get("https://chat.deepseek.com/")
                    await sleep(5)
                    
                    # Check again for textbox after forced navigation
                    if await self.find_textbox():
                        self.logger.debug("Found textbox after forced navigation - login was successful despite errors")
                        login_successful = True
                    else:
                        error_msg = "The email or password is incorrect" if not token_failed else "Both token and email/password are incorrect"
                        raise InvalidCredentials(error_msg)
                        
        except InvalidCredentials:
            raise
        except Exception as e:
            self.logger.error(f"Error during login process: {str(e)}")
            error_msg = "The email or password is incorrect" if not token_failed else "Both token and email/password are incorrect"
            raise InvalidCredentials(error_msg)
    
        self.logger.debug(f"Logged in successfully using email and password! {'(Token method failed)' if token_failed else ''}")
                
    async def _find_child_by_text(
        self,
        parent: zendriver.Element,
        text: str,
        in_depth: bool = False,
        depth_limit: int = 10
    ) -> Optional[zendriver.Element]:
        """Finds a child element by it's text.

        Args
        ---------
            parent (zendriver.Element): The parent element.
            text (str): The text to find.
            in_depth (bool): Whether to search in depth.
            depth_limit (int): The depth limit to search in.

        Returns
        ---------
            Optional[zendriver.Element]: The child element if found, otherwise None.

        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        if in_depth: # not the best way to do this, but it works
            for child in parent.children:
                if child.text_all.lower() == text.lower():
                    return child
                
                if depth_limit:
                    found = await self._find_child_by_text(child, text, in_depth, depth_limit - 1)
                    if found:
                        return found
        else:
            for child in parent.children:
                if child.text_all.lower() == text.lower():
                    return child
        
        return None

    async def retrieve_token(self) -> Optional[str]:
        """Retrieves the token from the browser's local storage.
        
        Returns
        ---------
            Optional[str]: The token if found, otherwise None.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")
        
        return await self.browser.main_tab.evaluate(
            "JSON.parse(localStorage.getItem('userToken')).value",
            await_promise = True,
            return_by_value = True
        )

    
    async def send_message(
        self,
        message: str,
        slow_mode: bool = False,
        deepthink: bool = False,
        search: bool = False,
        timeout: int = 60,
        slow_mode_delay: float = 0.25
    ) -> Optional[Response]:
        """Sends a message to the DeepSeek chat.
    
        Args
        ---------
            message (str): The message to send.
            slow_mode (bool): Whether to send the message character by character with a delay.
            deepthink (bool): Whether to enable deepthink mode.
                - Setting this to True will add 20 seconds to the timeout.
            search (bool): Whether to enable search mode.
                - Setting this to True will add 60 seconds to the timeout.
            timeout (int): The maximum time to wait for a response.
                - Sometimes a response may take longer than expected, so it's recommended to increase the timeout if necessary.
                - Do note that the timeout increases by 20 seconds if deepthink is enabled, and by 60 seconds if search is enabled.
            slow_mode_delay (float): The delay between sending each character in slow mode.
    
        Returns
        ---------
            Optional[Response]: The generated response from DeepSeek, or None if no response is received within the timeout
    
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """
    
        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")
    
        timeout += 20 if deepthink else 0
        timeout += 60 if search else 0
    
        self.logger.debug(f"Finding the textbox and sending the message: {message}")
        
        # Use dynamic element finder instead of fixed selector
        textbox = await self.find_textbox()
        
        if not textbox:
            self.logger.error("Could not find textbox")
            raise CouldNotFindElement("Could not find textbox")
            
        if slow_mode:
            for char in message:
                await textbox.send_keys(char)
                await sleep(slow_mode_delay)
        else:
            await textbox.send_keys(message)
    
        # Find the DeepThink and search options
        send_options = await self.find_send_options()
        
        # We need at least 2 options for DeepThink and Search
        if len(send_options) >= 2:
            # Assuming first option is DeepThink, second is Search (based on UI inspection)
            if deepthink != self._deepthink_enabled and len(send_options) > 0:
                await send_options[0].click()
                self._deepthink_enabled = deepthink
            
            if search != self._search_enabled and len(send_options) > 1:
                await send_options[1].click()
                self._search_enabled = search
        else:
            self.logger.warning("Could not find DeepThink/Search options, proceeding without them")
    
        # Use dynamic finder for send button
        send_button = await self.find_send_button()
        if not send_button:
            self.logger.error("Could not find send button")
            raise CouldNotFindElement("Could not find send button")
        
        await send_button.click()
    
        return await self._get_response(timeout=timeout)

    async def regenerate_response(self, timeout: int = 60) -> Optional[Response]:
        """Regenerates the response from DeepSeek.

        Args
        ---------
            timeout (int): The maximum time to wait for the response.

        Returns
        ---------
            Optional[Response]: The regenerated response from DeepSeek, or None if no response is received within the timeout
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
            ServerDown: If the server is busy and the response is not generated.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        # Find the last response so I can access it's buttons
        toolbar = await self.browser.main_tab.select_all(self.selectors.interactions.response_toolbar)
        await toolbar[-1].children[1].click()

        return await self._get_response(timeout = timeout, regen = True)
    
    def _filter_search_results(
        self,
        search_results_children: list,
    ):
        """Filters the search results and returns a list of SearchResult objects.

        Args
        ---------
            search_results_children (list): The search results children.

        Returns
        ---------
            list: A list of SearchResult objects.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        search_results = []
        for search_result in search_results_children:
            search_results.append(
                SearchResult(
                    image_url = BeautifulSoup(
                        str(search_result.children[0].children[0].children),
                        'html.parser'
                    ).find('img')['src'],
                    website = search_result.children[0].children[1].text_all,
                    date = search_result.children[0].children[2].text_all,
                    index = int(search_result.children[0].children[3].text_all),
                    title = search_result.children[1].text_all,
                    description = search_result.children[2].text_all
                )
            )
        
        return search_results

        # Update the _get_response method
    
    async def _get_response(
        self,
        timeout: int = 60,
        regen: bool = False,
    ) -> Optional[Response]:
        """Waits for and retrieves the response from DeepSeek.
    
        Args
        ---------
            timeout (int): The maximum time to wait for the response.
            regen (bool): Whether the response is a regenerated response.
    
        Returns
        ---------
            Optional[Response]: The generated response from DeepSeek, or None if no response is received within the timeout.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
            ServerDown: If the server is busy and the response is not generated.
        """
    
        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")
    
        end_time = time() + timeout
    
        # Find response elements dynamically
        response_elements = await self.find_response_elements()
        
        # Wait till the response starts generating
        self.logger.debug("Waiting for the response to start generating..." if not regen \
            else "Waiting for the response to start regenerating...")
        
        generating_indicator = None
        while time() < end_time:
            try:
                # Use dynamic finder or JavaScript check for response generation
                is_generating = await self._find_element_by_js("""
                (function() {
                    // Look for loading indicators
                    return !!document.querySelector('div[class*="loading"], div[class*="spinner"]');
                })()
                """)
                
                if is_generating:
                    generating_indicator = True
                    break
                
                await sleep(0.5)
            except:
                await sleep(0.5)
        
        if time() >= end_time or not generating_indicator:
            self.logger.warning("Could not detect generation starting within timeout")
            # Continue anyway as we might still get a response
        
        # Once the response starts generating, wait till it's generated
        self.logger.debug("Waiting for the response to finish generating...")
        
        # Check for response being completed
        response_text = None
        while time() < end_time:
            try:
                # Check if response is complete by seeing if loading indicators are gone
                is_still_generating = await self._find_element_by_js("""
                (function() {
                    return !!document.querySelector('div[class*="loading"], div[class*="spinner"]');
                })()
                """)
                
                if not is_still_generating:
                    # Try to get the response content
                    response_text = await self._find_element_by_js("""
                    (function() {
                        // Find message blocks which likely contain responses
                        const messageBlocks = Array.from(document.querySelectorAll(
                            'div[class*="message"], div[class*="chat-message"], div[class*="response"]'
                        ));
                        
                        if (messageBlocks.length === 0) return null;
                        
                        // Get the last message which is likely the response
                        const lastMessage = messageBlocks[messageBlocks.length - 1];
                        
                        // Extract text from all markdown blocks in the message
                        const markdownBlocks = lastMessage.querySelectorAll(
                            'div[class*="markdown"], pre, code, p'
                        );
                        
                        if (markdownBlocks.length > 0) {
                            return Array.from(markdownBlocks)
                                .map(block => block.innerText || block.textContent)
                                .join('\\n\\n');
                        }
                        
                        // If no specific markdown blocks, just get all text
                        return lastMessage.innerText || lastMessage.textContent;
                    })()
                    """)
                    
                    if response_text:
                        break
                
                await sleep(1)
            except Exception as e:
                self.logger.debug(f"Error while checking response: {str(e)}")
                await sleep(1)
        
        if not response_text:
            self.logger.warning("Could not extract response text within timeout")
            return None
    
        if response_text.lower() == "the server is busy. please try again later.":
            raise ServerDown("The server is busy. Please try again later.")
    
        # Check for deepthink and search results
        search_results = None
        deepthink_duration = None
        deepthink_content = None
        
        # Look for DeepThink content
        if self._deepthink_enabled:
            try:
                deepthink_info = await self._find_element_by_js("""
                (function() {
                    // Look for DeepThink duration indicator (e.g., "Thought for X seconds")
                    const durationElements = Array.from(document.querySelectorAll('div, span, p'))
                        .filter(el => {
                            const text = el.textContent.toLowerCase();
                            return text.includes('thought for') && text.includes('seconds');
                        });
                    
                    if (durationElements.length > 0) {
                        const durationText = durationElements[0].textContent;
                        const match = durationText.match(/thought for (\d+(\.\d+)?)/i);
                        const duration = match ? parseInt(match[1]) : null;
                        
                        // Look for DeepThink content
                        const parentContainer = durationElements[0].closest('div[class*="container"], div[class*="message"]');
                        let deepthinkContent = '';
                        
                        if (parentContainer) {
                            const contentElements = parentContainer.querySelectorAll('p, div[class*="content"]');
                            if (contentElements.length > 0) {
                                deepthinkContent = Array.from(contentElements)
                                    .map(el => el.innerText || el.textContent)
                                    .join('\\n');
                            }
                        }
                        
                        return { duration, content: deepthinkContent };
                    }
                    
                    return null;
                })()
                """)
                
                if deepthink_info:
                    deepthink_duration = deepthink_info.get('duration')
                    deepthink_content = deepthink_info.get('content')
            except Exception as e:
                self.logger.debug(f"Error extracting DeepThink info: {str(e)}")
        
        # Look for search results
        if self._search_enabled:
            try:
                search_results_data = await self._find_element_by_js("""
                (function() {
                    // Look for search results section
                    const searchHeaders = Array.from(document.querySelectorAll('div, h3, h4'))
                        .filter(el => {
                            const text = el.textContent.toLowerCase();
                            return text.includes('search') && text.includes('results');
                        });
                    
                    if (searchHeaders.length === 0) return null;
                    
                    // Find search results container
                    const searchContainer = searchHeaders[0].closest('div[class*="container"], div[class*="results"]');
                    if (!searchContainer) return null;
                    
                    // Extract search result items
                    const resultItems = Array.from(searchContainer.querySelectorAll('div[class*="result"], div[class*="item"]'));
                    
                    return resultItems.map(item => {
                        const img = item.querySelector('img');
                        const titleEl = item.querySelector('h3, h4, div[class*="title"]');
                        const descEl = item.querySelector('p, div[class*="description"]');
                        const metaElements = item.querySelectorAll('span, div[class*="meta"]');
                        
                        // Extract metadata (website, date, index)
                        let website = '';
                        let date = '';
                        let index = 0;
                        
                        if (metaElements.length >= 3) {
                            website = metaElements[0].textContent || '';
                            date = metaElements[1].textContent || '';
                            const indexMatch = metaElements[2].textContent.match(/\\d+/);
                            index = indexMatch ? parseInt(indexMatch[0]) : 0;
                        }
                        
                        return {
                            image_url: img ? img.src : '',
                            website,
                            date,
                            index,
                            title: titleEl ? (titleEl.innerText || titleEl.textContent) : '',
                            description: descEl ? (descEl.innerText || descEl.textContent) : ''
                        };
                    });
                })()
                """)
                
                if search_results_data and isinstance(search_results_data, list):
                    search_results = []
                    for item in search_results_data:
                        search_results.append(SearchResult(
                            image_url=item.get('image_url', ''),
                            website=item.get('website', ''),
                            date=item.get('date', ''),
                            index=item.get('index', 0),
                            title=item.get('title', ''),
                            description=item.get('description', '')
                        ))
            except Exception as e:
                self.logger.debug(f"Error extracting search results: {str(e)}")
    
        response = Response(
            text=response_text,
            chat_id=self._chat_id,
            deepthink_duration=deepthink_duration,
            deepthink_content=deepthink_content,
            search_results=search_results
        )
        
        self.logger.debug("Response generated!")
        return response
    
    async def reset_chat(self) -> None:
        """Resets the chat by clicking the reset button.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        reset_chat_button = await self.browser.main_tab.select(self.selectors.interactions.reset_chat_button)
        await reset_chat_button.click()
        self.chat_id = ""
        self.logger.debug("Chat reset!")
    
    async def logout(self) -> None:
        """Logs out of the DeepSeek account.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        self.logger.debug("Logging out...")
        await self.browser.main_tab.evaluate(
            "localStorage.removeItem('userToken')",
            await_promise = True,
            return_by_value = True
        )
        await self.browser.main_tab.reload()
        self.logger.debug("Logged out successfully!")
    
    async def switch_account(
        self,
        token: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None
    ) -> None:
        """Switches the account by logging out and logging back in with a new token.

        Args
        ---------
            token (Optional[str]): The new token to use.
            email (Optional[str]): The new email to use.
            password (Optional[str]): The new password to use.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method
            MissingCredentials: If neither the token nor the email and password are provided
            InvalidCredentials: If the token or email and password are incorrect
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        # Check if the token or email and password are provided
        if not token and not (email and password):
            raise MissingCredentials("Either the token alone or the email and password both must be provided")

        self.logger.debug("Switching the account...")

        # Log out of the current account
        await self.logout()

        # Update the credentials
        self._token = token
        self._email = email
        self._password = password

        if self._token:
            await self._login()
        else:
            await self._login_classic()
        
    async def delete_chats(self) -> None:
        """Deletes all the chats in the chat.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
            CouldNotFindElement: If the delete chats button is not found.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        self.logger.debug("Clicking the profile button...")
        profile_button = await self.browser.main_tab.select(self.selectors.interactions.profile_button)
        await profile_button.click()
        
        self.logger.debug("Clicking the profile options dropdown...")
        profile_options_dropdown = await self.browser.main_tab.select(self.selectors.interactions.profile_options_dropdown)
        await profile_options_dropdown.click()

        self.logger.debug("Finding and clicking the delete chats button...")
        delete_chats_button = await self._find_child_by_text(
            parent = profile_options_dropdown,
            text = "Delete all chats",
            in_depth = True
        )
        if not delete_chats_button:
            raise CouldNotFindElement("Could not find the delete chats button")

        await delete_chats_button.click()

        self.logger.debug("Clicking the confirm deletion button...")
        confirm_deletion_button = await self.browser.main_tab.select(self.selectors.interactions.confirm_deletion_button)
        await confirm_deletion_button.click()

        self.logger.debug("chats deleted!")
    
    async def switch_chat(self, chat_id: str) -> None:
        """Switches the chat by navigating to a new chat id.

        Args
        ---------
            chat_id (str): The new chat id to navigate to.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
            InvalidChatID: If the chat id is invalid
            CouldNotFindElement: If the textbox is not found
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        self.logger.debug(f"Switching the chat to: {chat_id}")
        await self.browser.main_tab.get(f"https://chat.deepseek.com/a/chat/s/{chat_id}")

        # Wait till text box appears
        self.logger.debug("Waiting for the textbox to appear...")
        try:
            await self.browser.main_tab.wait_for(self.selectors.interactions.textbox, timeout = 5)
        except:
            raise CouldNotFindElement("Could not find the textbox")

        chat_id_in_url = await self.browser.main_tab.evaluate(
            f"window.location.href.includes('{chat_id}')",
            await_promise = True,
            return_by_value = True
        )

        if not chat_id_in_url:
            raise InvalidChatID("The chat id is invalid")
        
        self._chat_id = chat_id
        self.logger.debug("Chat switched!")
    
    async def switch_theme(self, theme: Theme):
        """Switches the theme of the chat.

        Args
        ---------
            theme (Theme): The theme to switch to.
        
        Raises
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
        """

        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")

        self.logger.debug(f"Switching the theme to: {theme.value}")
        await self.browser.main_tab.evaluate(
            f"localStorage.setItem('__appKit_@deepseek/chat_themePreference', JSON.stringify({{value: '{theme.value}', __version: '0'}}))",
            await_promise = True,
            return_by_value = True
        )

        await self.browser.main_tab.reload()
        self.logger.debug("Theme switched!")


        
    async def _find_element_by_js(self, js_search_function: str) -> Optional[str]:
            """Uses JavaScript to find elements in a more dynamic way.
            
            Args:
                js_search_function: JavaScript function string that returns an element or null
            
            Returns:
                Optional CSS selector for the found element
            """
            if not self._initialized:
                raise MissingInitialization("You must run the initialize method before using this method.")
                
            result = await self.browser.main_tab.evaluate(
                js_search_function,
                await_promise=True,
                return_by_value=True
            )
            return result
        
    async def find_textbox(self) -> Optional[zendriver.Element]:
            """Dynamically finds the chat input textbox."""
            selector = await self._find_element_by_js("""
            (function() {
                // Try to find the chat input
                const textareas = Array.from(document.querySelectorAll('textarea'));
                
                // Look for chat input by placeholder text first
                const chatInput = textareas.find(el => 
                    el.placeholder && 
                    (el.placeholder.toLowerCase().includes("message") || 
                     el.placeholder.toLowerCase().includes("chat") ||
                     el.placeholder.toLowerCase().includes("ask"))
                );
                
                if (chatInput) return chatInput.tagName.toLowerCase() + 
                    (chatInput.id ? `#${chatInput.id}` : '') + 
                    (chatInput.className ? `.${chatInput.className.split(' ')[0]}` : '');
                
                // If no specialized textarea found, find the most prominent one
                // (typically the one with the largest height or in the bottom part of page)
                if (textareas.length > 0) {
                    // Sort by position from bottom and size
                    textareas.sort((a, b) => {
                        const aRect = a.getBoundingClientRect();
                        const bRect = b.getBoundingClientRect();
                        // Prefer elements closer to bottom of page and with larger area
                        const aScore = (window.innerHeight - aRect.bottom) + (aRect.height * aRect.width * 0.01);
                        const bScore = (window.innerHeight - bRect.bottom) + (bRect.height * bRect.width * 0.01);
                        return aScore - bScore;
                    });
                    
                    const bestTextarea = textareas[0];
                    return bestTextarea.tagName.toLowerCase() + 
                        (bestTextarea.id ? `#${bestTextarea.id}` : '') + 
                        (bestTextarea.className ? `.${bestTextarea.className.split(' ')[0]}` : '');
                }
                
                // Try contenteditable divs if no textareas found
                const editableDivs = Array.from(document.querySelectorAll('div[contenteditable="true"]'));
                if (editableDivs.length > 0) {
                    const bestDiv = editableDivs[0];
                    return bestDiv.tagName.toLowerCase() + 
                        (bestDiv.id ? `#${bestDiv.id}` : '') + 
                        (bestDiv.className ? `.${bestDiv.className.split(' ')[0]}` : '');
                }
                
                return null;
            })()
            """)
            
            if selector:
                self.logger.debug(f"Found textbox with selector: {selector}")
                try:
                    return await self.browser.main_tab.select(selector, timeout=5)
                except:
                    self.logger.error(f"Failed to select textbox with selector: {selector}")
            
            # Fallback to direct search
            try:
                return await self.browser.main_tab.select('textarea', timeout=5)
            except:
                self.logger.error("Could not find textbox with any method")
                return None
        
    async def find_send_button(self) -> Optional[zendriver.Element]:
            """Dynamically finds the send button."""
            selector = await self._find_element_by_js("""
            (function() {
                // Look for send button by various attributes
                const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                
                // First try buttons with send-related text
                const sendButton = buttons.find(el => {
                    const text = el.textContent.toLowerCase();
                    return text.includes('send') || text === '↵' || text === '→' || text === '⏎';
                });
                
                if (sendButton) return sendButton.tagName.toLowerCase() + 
                    (sendButton.id ? `#${sendButton.id}` : '') + 
                    (sendButton.className ? `.${sendButton.className.split(' ')[0]}` : '');
                
                // Next, look for buttons with send-related attributes
                const attrButton = buttons.find(el => 
                    (el.getAttribute('aria-label') && 
                     el.getAttribute('aria-label').toLowerCase().includes('send')) ||
                    (el.title && el.title.toLowerCase().includes('send'))
                );
                
                if (attrButton) return attrButton.tagName.toLowerCase() + 
                    (attrButton.id ? `#${attrButton.id}` : '') + 
                    (attrButton.className ? `.${attrButton.className.split(' ')[0]}` : '');
                
                // If no specialized button, look for button next to the textarea
                const textarea = document.querySelector('textarea');
                if (textarea) {
                    const closestButton = buttons.sort((a, b) => {
                        const aRect = a.getBoundingClientRect();
                        const bRect = b.getBoundingClientRect();
                        const textareaRect = textarea.getBoundingClientRect();
                        
                        // Calculate distance to textarea
                        const aDist = Math.sqrt(
                            Math.pow(aRect.left - textareaRect.right, 2) + 
                            Math.pow(aRect.top - textareaRect.top, 2)
                        );
                        const bDist = Math.sqrt(
                            Math.pow(bRect.left - textareaRect.right, 2) + 
                            Math.pow(bRect.top - textareaRect.top, 2)
                        );
                        
                        return aDist - bDist;
                    })[0];
                    
                    if (closestButton) return closestButton.tagName.toLowerCase() + 
                        (closestButton.id ? `#${closestButton.id}` : '') + 
                        (closestButton.className ? `.${closestButton.className.split(' ')[0]}` : '');
                }
                
                // If all else fails, try to find a button with an icon
                const iconButtons = buttons.filter(el => el.querySelector('svg, img'));
                if (iconButtons.length > 0) {
                    // Take the last one as it's often the send button
                    const iconButton = iconButtons[iconButtons.length - 1];
                    return iconButton.tagName.toLowerCase() + 
                        (iconButton.id ? `#${iconButton.id}` : '') + 
                        (iconButton.className ? `.${iconButton.className.split(' ')[0]}` : '');
                }
                
                return null;
            })()
            """)
            
            if selector:
                self.logger.debug(f"Found send button with selector: {selector}")
                try:
                    return await self.browser.main_tab.select(selector, timeout=5)
                except:
                    self.logger.error(f"Failed to select send button with selector: {selector}")
            
            # Fallback to direct search
            try:
                return await self.browser.main_tab.select('div[role="button"]', timeout=5)
            except:
                self.logger.error("Could not find send button with any method")
                return None
