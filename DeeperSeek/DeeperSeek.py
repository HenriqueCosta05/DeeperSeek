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
        """Logs in to DeepSeek using email and password.
    
        Args
        ---------
            token_failed (bool): Indicates whether the token login attempt failed.
        
        Raises:
        ---------
            MissingInitialization: If the initialize method is not run before using this method.
            InvalidCredentials: If the email or password is incorrect.
        """
    
        if not self._initialized:
            raise MissingInitialization("You must run the initialize method before using this method.")
        
        self.logger.debug("Attempting to login with email and password...")
            
        # 1. Wait longer for the page to fully load
        try:
            await sleep(3)  # Add a small delay to ensure page is loaded
            await self.browser.main_tab.evaluate(
                "document.readyState === 'complete'",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug("Page loading complete")
        except Exception as e:
            self.logger.error(f"Page loading check failed: {str(e)}")
            
        # 2. Capture page source for debugging
        try:
            page_source = await self.browser.main_tab.evaluate(
                    "document.body.innerHTML",
                    await_promise=True,
                    return_by_value=True
                )
            self.logger.debug(f"Login page structure found, size: {len(page_source)} bytes")
            
            # Look for login form elements
            login_form_exists = await self.browser.main_tab.evaluate(
                """
                !!document.querySelector('input[type="text"]') && 
                !!document.querySelector('input[type="password"]')
                """,
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug(f"Login form detected: {login_form_exists}")
        except Exception as e:
            self.logger.error(f"Failed to get page source: {str(e)}")
        
        # 3. Find and interact with the email input - use more generic selector
        try:
            self.logger.debug("Looking for email input field...")
            email_input = await self.browser.main_tab.evaluate(
                """
                (function() {
                    // Try different methods to find the email field
                    const emailField = document.querySelector('input[type="text"]') || 
                                       document.querySelector('input[type="email"]') ||
                                       document.querySelector('input[placeholder*="email" i]');
                    if (emailField) {
                        emailField.focus();
                        return true;
                    }
                    return false;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if email_input:
                self.logger.debug("Email input found via JS, entering email...")
                await self.browser.main_tab.evaluate(
                    f'document.activeElement.value = "{self._email}";',
                    await_promise=True,
                    return_by_value=True
                )
                self.logger.debug("Email entered successfully")
            else:
                self.logger.error("Could not find email input field with JavaScript")
                raise InvalidCredentials("Could not find email input field")
        except Exception as e:
            self.logger.error(f"Failed to enter email: {str(e)}")
            raise InvalidCredentials(f"Could not find email input field: {str(e)}")
        
        # 4. Find and interact with the password input
        try:
            self.logger.debug("Looking for password input field...")
            password_input = await self.browser.main_tab.evaluate(
                """
                (function() {
                    // Try to find and focus the password field
                    const pwField = document.querySelector('input[type="password"]');
                    if (pwField) {
                        pwField.focus();
                        return true;
                    }
                    return false;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if password_input:
                self.logger.debug("Password input found via JS, entering password...")
                await self.browser.main_tab.evaluate(
                    f'document.activeElement.value = "{self._password}";',
                    await_promise=True,
                    return_by_value=True
                )
                self.logger.debug("Password entered successfully")
            else:
                self.logger.error("Could not find password input field with JavaScript")
                raise InvalidCredentials("Could not find password input field")
        except Exception as e:
            self.logger.error(f"Failed to enter password: {str(e)}")
            raise InvalidCredentials(f"Could not find password input field: {str(e)}")
        
        # 5. Handle any checkbox in a more robust way
        try:
            self.logger.debug("Attempting to check any required checkboxes via JavaScript...")
            await self.browser.main_tab.evaluate(
                """
                // More comprehensive checkbox finder and clicker
                (function() {
                    // Find all possible checkbox elements
                    const checkboxSelectors = [
                        'input[type="checkbox"]', 
                        'div[class*="checkbox"]', 
                        'div[class*="ds-checkbox"]',
                        'label.checkbox',
                        '*[role="checkbox"]'
                    ];
                    
                    // Try each selector
                    for (const selector of checkboxSelectors) {
                        const elements = document.querySelectorAll(selector);
                        if (elements.length > 0) {
                            console.log('Found checkbox elements with selector: ' + selector);
                            // Click all found elements
                            elements.forEach(el => {
                                try {
                                    el.click();
                                    console.log('Clicked checkbox element');
                                } catch (e) {
                                    console.log('Error clicking checkbox:', e);
                                }
                            });
                        }
                    }
                    return true;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug("JavaScript checkbox handling completed")
        except Exception as e:
            self.logger.error(f"JavaScript checkbox handling failed: {str(e)}")
            # Continue anyway as the checkbox might not be required
        
        # 6. Find and click the login button using a more robust approach
        try:
            self.logger.debug("Looking for login button...")
            button_clicked = await self.browser.main_tab.evaluate(
                """
                (function() {
                    // Try multiple approaches to find the login button
                    const buttonSelectors = [
                        'button[type="submit"]',
                        'div[role="button"]',
                        'button:not([disabled])',
                        'input[type="submit"]',
                        'a.login-button',
                        // Text-based selectors
                        'button:contains("Login")', 
                        'button:contains("Sign In")',
                        'div[role="button"]:contains("Login")',
                        'div[role="button"]:contains("Sign In")'
                    ];
                    
                    // Custom contains selector implementation
                    function findElementsWithText(selector, text) {
                        const elements = document.querySelectorAll(selector);
                        return Array.from(elements).filter(el => 
                            el.textContent.toLowerCase().includes(text.toLowerCase()));
                    }
                    
                    // Try standard selectors
                    for (const selector of buttonSelectors) {
                        if (selector.includes(':contains(')) {
                            // Handle our custom text-based selector
                            const [baseSelector, textToFind] = selector.split(':contains(');
                            const text = textToFind.replace('"', '').replace('")', '');
                            const elements = findElementsWithText(baseSelector, text);
                            if (elements.length > 0) {
                                elements[0].click();
                                return true;
                            }
                        } else {
                            // Standard selector
                            const elements = document.querySelectorAll(selector);
                            if (elements.length > 0) {
                                elements[0].click();
                                return true;
                            }
                        }
                    }
                    
                    // If nothing found, look for any button-like element
                    const allButtons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
                    if (allButtons.length > 0) {
                        // Click the last button as it's often the submit button
                        allButtons[allButtons.length - 1].click();
                        return true;
                    }
                    
                    return false;
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if button_clicked:
                self.logger.debug("Login button found and clicked via JS")
            else:
                self.logger.error("Could not find login button with JavaScript")
                raise InvalidCredentials("Could not find or click login button")
        except Exception as e:
            self.logger.error(f"Failed to click login button: {str(e)}")
            raise InvalidCredentials(f"Could not find or click login button: {str(e)}")
        
        # Replace lines around 392-451 (the section for waiting for successful login)

        # 7. Wait for successful login with increased patience
        self.logger.debug("Waiting for login to complete...")
        try:
            # Try several selectors that might indicate successful login
            await sleep(10)  # Increased wait time after login button click
            
            # First check if we're redirected to a different URL that indicates success
            current_url = await self.browser.main_tab.evaluate(
                "window.location.href",
                await_promise=True,
                return_by_value=True
            )
            self.logger.debug(f"Current URL after login: {current_url}")
            
            # Check if URL indicates we're past login screen (could be welcome, chat, or dashboard)
            url_indicates_success = await self.browser.main_tab.evaluate(
                """
                (function() {
                    const url = window.location.href;
                    // Check for various success indicators in URL
                    return url.includes('/chat') || 
                        url.includes('/welcome') || 
                        url.includes('/dashboard') ||
                        url.includes('/home') ||
                        !url.includes('/login');
                })()
                """,
                await_promise=True,
                return_by_value=True
            )
            
            if url_indicates_success:
                self.logger.debug("Login appears successful based on URL change")
                login_successful = True
            else:
                # If URL doesn't indicate success, look for UI elements
                login_successful = await self.browser.main_tab.evaluate(
                    """
                    (function() {
                        // Check for ANY of these indicators of successful login
                        
                        // 1. Check for any textbox
                        if (document.querySelectorAll('textarea').length > 0) return true;
                        
                        // 2. Check for elements that would only be shown to logged-in users
                        if (document.querySelectorAll('div[class*="profile"], div[class*="avatar"], div[class*="user"]').length > 0) return true;
                        
                        // 3. Check for chat-related elements
                        if (document.querySelectorAll('div[class*="chat"], div[class*="message"], div[class*="conversation"]').length > 0) return true;
                        
                        // 4. Check for welcome screens or onboarding elements
                        if (document.querySelectorAll('div[class*="welcome"], div[class*="onboarding"], div[class*="getting-started"]').length > 0) return true;
                        
                        // 5. Check for navigation elements that appear post-login
                        if (document.querySelectorAll('div[class*="sidebar"], div[class*="nav"], div[class*="menu"]').length > 0) return true;
                        
                        // 6. Check if login form is gone
                        if (!document.querySelector('input[type="password"]')) return true;
                        
                        return false;
                    })()
                    """,
                    await_promise=True,
                    return_by_value=True
                )
            
            if login_successful:
                self.logger.debug("Login successful - authenticated interface detected")
                
                # If we're on a welcome/onboarding page, we need to navigate to the chat
                try:
                    if not await self.find_textbox():
                        self.logger.debug("No textbox found on current page, attempting to navigate to chat")
                        
                        # Try to find a "Start Chat" or similar button
                        start_chat_clicked = await self.browser.main_tab.evaluate(
                            """
                            (function() {
                                // Find buttons that might lead to chat
                                const chatButtons = Array.from(
                                    document.querySelectorAll('button, div[role="button"], a')
                                ).filter(el => {
                                    const text = el.textContent.toLowerCase();
                                    return (
                                        text.includes('start') || 
                                        text.includes('chat') || 
                                        text.includes('continue') || 
                                        text.includes('next') ||
                                        text.includes('begin')
                                    );
                                });
                                
                                if (chatButtons.length > 0) {
                                    chatButtons[0].click();
                                    return true;
                                }
                                
                                // As a fallback, try to navigate directly to chat URL
                                try {
                                    window.location.href = 'https://chat.deepseek.com/';
                                    return true;
                                } catch (e) {
                                    return false;
                                }
                            })()
                            """,
                            await_promise=True,
                            return_by_value=True
                        )
                        
                        if start_chat_clicked:
                            self.logger.debug("Clicked a button to navigate to chat")
                            await sleep(5)  # Wait for navigation
                        else:
                            self.logger.debug("No chat navigation button found, trying direct URL")
                            await self.browser.main_tab.get("https://chat.deepseek.com/")
                            await sleep(5)
                    
                    # Now check again for textbox
                    if await self.find_textbox():
                        self.logger.debug("Chat textbox found after navigation")
                    else:
                        self.logger.debug("Still no textbox found, but login appears successful")
                except Exception as e:
                    self.logger.error(f"Error while trying to navigate to chat: {str(e)}")
                    # Continue anyway as login might be successful
            else:
                # Check for error messages
                error_present = await self.browser.main_tab.evaluate(
                    """
                    (function() {
                        const errorElements = document.querySelectorAll(
                            'div[class*="error"], p[class*="error"], span[class*="error"], .notification-error, .error-message'
                        );
                        for (const el of errorElements) {
                            if (el.textContent && (
                                el.textContent.includes('incorrect') || 
                                el.textContent.includes('invalid') ||
                                el.textContent.includes('failed') ||
                                el.textContent.includes('wrong'))) {
                                return el.textContent.trim();
                            }
                        }
                        return false;
                    })()
                    """,
                    await_promise=True,
                    return_by_value=True
                )
                
                if error_present:
                    self.logger.error(f"Login error detected: {error_present}")
                    raise InvalidCredentials(f"Login error: {error_present}")
                else:
                    self.logger.error("Login failed - could not detect login success")
                    
                    # Capture full page source for detailed debugging
                    page_html = await self.browser.main_tab.evaluate(
                        "document.documentElement.outerHTML",
                        await_promise=True,
                        return_by_value=True
                    )
                    
                    self.logger.debug(f"Failed login page structure, size: {len(page_html)} bytes")
                    self.logger.debug(f"Current URL: {await self.browser.main_tab.evaluate('window.location.href', await_promise=True, return_by_value=True)}")
                    
                    # Last attempt: try forced navigation to chat
                    try:
                        self.logger.debug("Attempting forced navigation to chat as last resort")
                        await self.browser.main_tab.get("https://chat.deepseek.com/")
                        await sleep(5)
                        
                        if await self.find_textbox():
                            self.logger.debug("Found textbox after forced navigation - login was likely successful")
                            login_successful = True
                        else:
                            error_msg = "The email or password is incorrect" if not token_failed else "Both token and email/password are incorrect"
                            raise InvalidCredentials(error_msg)
                    except InvalidCredentials:
                        raise
                    except Exception as e:
                        self.logger.error(f"Final navigation attempt failed: {str(e)}")
                        error_msg = "The email or password is incorrect" if not token_failed else "Both token and email/password are incorrect"
                        raise InvalidCredentials(error_msg)
        except InvalidCredentials:
            # Re-raise the specific exception
            raise
        except Exception as e:
            self.logger.error(f"Error while checking login status: {str(e)}")
            
            # Capture page source after failed login for debugging
            try:
                failed_page = await self.browser.main_tab.evaluate(
                    "document.body.innerHTML",
                    await_promise=True,
                    return_by_value=True
                )
                self.logger.debug(f"Failed login page structure, size: {len(failed_page)} bytes")
            except:
                pass
                
            # Try forced navigation as last resort
            try:
                self.logger.debug("Attempting forced navigation to chat as last resort after error")
                await self.browser.main_tab.get("https://chat.deepseek.com/")
                await sleep(5)
                
                if await self.find_textbox():
                    self.logger.debug("Found textbox after forced navigation - login was successful despite errors")
                    login_successful = True
                else:
                    error_msg = "The email or password is incorrect" if not token_failed else "Both token and email/password are incorrect"
                    raise InvalidCredentials(error_msg)
            except InvalidCredentials:
                raise
            except Exception as nav_error:
                self.logger.error(f"Final navigation attempt failed: {str(nav_error)}")
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
