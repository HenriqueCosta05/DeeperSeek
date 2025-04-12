from dataclasses import dataclass, field
from typing import Optional

@dataclass
class LoginSelectors:
    # These should be more stable as they are standard HTML form elements
    email_input: str = 'input[type="text"], input[type="email"]'
    password_input: str = 'input[type="password"]'
    # No longer needed as we'll handle this with JavaScript
    confirm_checkbox: str = None
    # More general button selector
    login_button: str = 'button[type="submit"], div[role="button"]'

@dataclass
class InteractionSelectors:
    # More generic textarea selector
    textbox: str = 'textarea, div[contenteditable="true"]'
    # Will use JavaScript instead of these fixed selectors
    send_options_parent: str = None
    send_button: str = None
    response_toolbar: str = None
    reset_chat_button: str = None
    search_results: str = None
    deepthink_content: str = None
    profile_button: str = None
    profile_options_dropdown: str = None
    confirm_deletion_button: str = None
    theme_select_parent: str = None

@dataclass
class BackendSelectors:
    # Will use JavaScript instead of these fixed selectors
    response_generating: str = None
    response_generated: str = None
    regen_loading_icon: str = None
    response_toolbar_b64: str = None

@dataclass
class URLSelectors:
    chat_url: str = "https://chat.deepseek.com/"

@dataclass
class DeepSeekSelectors:
    login: LoginSelectors = field(default_factory=LoginSelectors)
    interactions: InteractionSelectors = field(default_factory=InteractionSelectors)
    backend: BackendSelectors = field(default_factory=BackendSelectors)
    urls: URLSelectors = field(default_factory=URLSelectors)
