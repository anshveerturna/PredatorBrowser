"""
Example usage of the Predator Browser.

This script demonstrates the core functionality of the Predator Browser,
showing how to use the Waterfall Cost-Logic for various web interactions.
"""

import asyncio
import logging
import os

from app.core.predator import PredatorBrowser, BrowserConfig

# Configure logging to see the Waterfall in action
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


async def example_basic_navigation():
    """Example: Basic navigation and goal execution."""
    print("\n" + "="*60)
    print("Example 1: Basic Navigation & Goal Execution")
    print("="*60)
    
    config = BrowserConfig(
        headless=True,
        stealth_mode=True
    )
    
    async with PredatorBrowser(config).session() as browser:
        # Navigate to a website
        await browser.navigate("https://news.ycombinator.com")
        
        # Get page info
        info = await browser.get_page_info()
        print(f"\nPage Info: {info}")
        
        # Execute a goal - the browser will try L1, then L2, then L3
        result = await browser.execute_goal("Find the top story headline")
        
        print(f"\nResult: {result.to_dict()}")
        print(f"Level used: {result.level.value}")


async def example_data_extraction():
    """Example: Extracting structured data from a page."""
    print("\n" + "="*60)
    print("Example 2: Data Extraction")
    print("="*60)
    
    config = BrowserConfig(headless=True)
    
    async with PredatorBrowser(config).session() as browser:
        await browser.navigate("https://news.ycombinator.com")
        
        # Extract structured data
        result = await browser.extract_data({
            "top_stories": "array of strings",
            "points": "array of numbers"
        })
        
        print(f"\nExtracted Data: {result.data}")
        print(f"Level used: {result.level.value}")


async def example_form_interaction():
    """Example: Interacting with forms."""
    print("\n" + "="*60)
    print("Example 3: Form Interaction")
    print("="*60)
    
    config = BrowserConfig(headless=True)
    
    async with PredatorBrowser(config).session() as browser:
        await browser.navigate("https://duckduckgo.com")
        
        # Type into search box
        result = await browser.type_text("Search the web", "predator browser")
        print(f"\nType Result: {result.to_dict()}")
        
        # Click search button
        result = await browser.click("Search button")
        print(f"\nClick Result: {result.to_dict()}")
        
        # Get new page info
        info = await browser.get_page_info()
        print(f"\nNew URL: {info['url']}")


async def example_network_interception():
    """Example: Demonstrating Level 1 (Shadow API) capabilities."""
    print("\n" + "="*60)
    print("Example 4: Network Interception (Shadow API)")
    print("="*60)
    
    config = BrowserConfig(headless=True)
    
    async with PredatorBrowser(config).session() as browser:
        # Navigate to a site with API calls
        await browser.navigate("https://api.github.com")
        
        # Check what's been captured
        if browser.sniffer:
            summary = browser.sniffer.get_buffer_summary()
            print(f"\nCaptured {len(summary)} API responses:")
            for item in summary[:5]:
                print(f"  - {item['url'][:80]}...")


async def example_accessibility_tree():
    """Example: Using the Accessibility Tree (Level 2)."""
    print("\n" + "="*60)
    print("Example 5: Accessibility Tree Navigation")
    print("="*60)
    
    config = BrowserConfig(headless=True)
    
    async with PredatorBrowser(config).session() as browser:
        await browser.navigate("https://example.com")
        
        # Get condensed AX tree
        if browser.navigator:
            tree = await browser.navigator.get_condensed_tree(max_nodes=50)
            print(f"\nAccessibility Tree (first 50 nodes):")
            print(tree[:1000])  # First 1000 chars
            
            # Get interactive elements
            elements = await browser.navigator.get_interactive_elements()
            print(f"\nFound {len(elements)} interactive elements")


async def example_vision_mode():
    """Example: Using Vision with Set-of-Marks (Level 3)."""
    print("\n" + "="*60)
    print("Example 6: Vision Mode (Set-of-Marks)")
    print("="*60)
    
    config = BrowserConfig(headless=True)
    
    async with PredatorBrowser(config).session() as browser:
        await browser.navigate("https://example.com")
        
        # Get marked screenshot
        if browser.vision:
            # Get bounding boxes of interactive elements
            boxes = await browser.vision.get_interactive_bounding_boxes()
            print(f"\nFound {len(boxes)} interactive elements for marking")
            
            for box in boxes[:5]:
                print(f"  - [{box.role}] \"{box.name}\" at ({box.x:.0f}, {box.y:.0f})")


async def main():
    """Run all examples."""
    print("\n" + "#"*60)
    print("# PREDATOR BROWSER - USAGE EXAMPLES")
    print("# The Information Interceptor")
    print("#"*60)
    
    # Check for API key
    if not os.getenv("OPENAI_API_KEY"):
        print("\n⚠️  Warning: OPENAI_API_KEY not set!")
        print("   Some features require OpenAI API access.")
        print("   Set the environment variable to enable full functionality.")
        print("\n   export OPENAI_API_KEY='your-key-here'\n")
        return
    
    try:
        await example_basic_navigation()
    except Exception as e:
        print(f"Example 1 failed: {e}")
    
    try:
        await example_accessibility_tree()
    except Exception as e:
        print(f"Example 5 failed: {e}")
    
    # Uncomment to run additional examples:
    # await example_data_extraction()
    # await example_form_interaction()
    # await example_network_interception()
    # await example_vision_mode()
    
    print("\n" + "="*60)
    print("Examples completed!")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
