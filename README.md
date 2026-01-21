# Predator Browser 🦅

**Enterprise Agentic Browser Module** - An information interceptor that follows the Waterfall Cost-Logic for maximum efficiency.

## Philosophy

The Predator Browser does NOT act like a standard scraper. It is an **information interceptor** that follows a strict hierarchy:

| Level | Name | Method | Cost | Speed |
|-------|------|--------|------|-------|
| **L1** | Shadow API | Network Interception | 0 | ⚡⚡⚡ |
| **L2** | Blind Map | Accessibility Tree | Low | ⚡⚡ |
| **L3** | Eagle Eye | Vision + Set-of-Marks | High | ⚡ |

## Quick Start

### Prerequisites

- Python 3.11+
- OpenAI API Key
- Docker (optional)

### Installation

```bash
# Clone the repository
cd PredatorBrowser

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Set your OpenAI API key
export OPENAI_API_KEY="your-key-here"
```

### Running as MCP Server

```bash
# Start the MCP server
python -m app.server
```

### Using with Claude Desktop

Add to your Claude Desktop MCP configuration:

```json
{
    "mcpServers": {
        "predator-browser": {
            "command": "python",
            "args": ["-m", "app.server"],
            "cwd": "/path/to/PredatorBrowser",
            "env": {
                "OPENAI_API_KEY": "your-key"
            }
        }
    }
}
```

### Docker

```bash
# Build the image
docker build -t predator-browser .

# Run with docker-compose
OPENAI_API_KEY=your-key docker-compose up
```

## Architecture

```
app/
├── __init__.py
├── server.py                 # MCP Server entry point
└── core/
    ├── __init__.py
    ├── predator.py          # Main orchestrator (Waterfall Logic)
    └── levels/
        ├── __init__.py
        ├── sniffer.py       # Level 1: Network interception
        ├── navigator.py     # Level 2: Accessibility Tree
        └── vision.py        # Level 3: Set-of-Marks
```

## MCP Tools

### `browse`
Navigate to a URL and execute a goal using the Waterfall Cost-Logic.

```json
{
    "url": "https://example.com",
    "goal": "Find the price of the first product"
}
```

### `click`
Smart click using Level 2 first, then Level 3.

```json
{
    "element_description": "Submit button"
}
```

### `type`
Type text into an input field.

```json
{
    "field_description": "Email address",
    "text": "user@example.com"
}
```

### `extract_data`
Extract structured data from the page.

```json
{
    "schema": {
        "title": "string",
        "price": "number",
        "description": "string"
    }
}
```

### `screenshot`
Capture the current page.

```json
{
    "full_page": false,
    "marked": true  // Apply Set-of-Marks annotations
}
```

### `get_ax_tree`
Get the Accessibility Tree for debugging.

### `get_network_log`
View captured API responses.

## How It Works

### Level 1: Shadow API (Sniffer)
- Listens to all network traffic via `page.on("response")`
- Maintains a rolling buffer of the last 50 JSON responses
- Uses a fast LLM to match captured data to the goal
- **Zero DOM interaction** = Maximum speed & reliability

### Level 2: Blind Map (Navigator)
- Uses `page.accessibility.snapshot()` to get the AX Tree
- Converts to a condensed markdown format for LLM processing
- Finds elements by role, name, and context
- Uses semantic selectors for reliable interaction

### Level 3: Eagle Eye (Vision)
- Takes a screenshot and identifies interactive elements
- Applies **Set-of-Marks** (red boxes with numbers)
- Sends to GPT-4V for visual understanding
- Clicks by coordinates as last resort

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OPENAI_API_KEY` | Required | Your OpenAI API key |
| `PREDATOR_HEADLESS` | `true` | Run browser headless |
| `PREDATOR_STEALTH` | `true` | Enable bot detection evasion |
| `PREDATOR_ROUTER_MODEL` | `gpt-4o-mini` | Fast model for routing |
| `PREDATOR_VISION_MODEL` | `gpt-4o` | Vision model for L3 |
| `PREDATOR_VIEWPORT_WIDTH` | `1920` | Browser viewport width |
| `PREDATOR_VIEWPORT_HEIGHT` | `1080` | Browser viewport height |

## Programmatic Usage

```python
import asyncio
from app.core.predator import PredatorBrowser, BrowserConfig

async def main():
    config = BrowserConfig(
        headless=True,
        stealth_mode=True
    )
    
    async with PredatorBrowser(config).session() as browser:
        # Navigate to a page
        await browser.navigate("https://example.com")
        
        # Execute a goal (uses Waterfall Logic)
        result = await browser.execute_goal("Find the main heading")
        print(result.to_dict())
        
        # Smart click
        result = await browser.click("Learn More button")
        
        # Type into a field
        result = await browser.type_text("Search box", "hello world")
        
        # Extract structured data
        result = await browser.extract_data({
            "title": "string",
            "links": "array"
        })

asyncio.run(main())
```

## Stealth Features

The Predator Browser implements multiple stealth techniques:

- ✅ WebDriver property masking
- ✅ Navigator plugins spoofing
- ✅ Chrome runtime object injection
- ✅ WebGL renderer spoofing
- ✅ Permission query override
- ✅ Custom user agent rotation
- ✅ Realistic HTTP headers

## Error Handling

The browser implements automatic retry with level promotion:

1. If Level 1 fails → Promote to Level 2
2. If Level 2 fails → Promote to Level 3
3. If Level 3 fails → Return detailed error

Each level captures specific errors (`TimeoutError`, `ElementHandleError`) for debugging.

## Logging

Detailed logs show the Waterfall status:

```
[INFO] [Predator] ═══ Executing Goal: 'Find the price' ═══
[INFO] [Predator] → Level 1: Checking Shadow API (Network Traffic)...
[INFO] [Sniffer] Analyzing 12 captured responses for goal: 'Find the price'
[INFO] [Sniffer] ✓ Successfully extracted data from Shadow API
[INFO] [Predator] ✓ Goal achieved at Level 1 in 45ms
```

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions are welcome! Please read the contributing guidelines first.
