import asyncio
import random


async def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    """Sleep for a random duration to simulate human behavior."""
    delay = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(delay)


async def human_type(page, selector: str, text: str, delay_range: tuple = (50, 150)):
    """Type text character by character with random delays between keystrokes."""
    element = await page.query_selector(selector)
    if element:
        await element.click()
        for char in text:
            await page.keyboard.press(char)
            await asyncio.sleep(random.uniform(delay_range[0], delay_range[1]) / 1000)


async def human_scroll(page, distance: int = 300, steps: int = 3):
    """Scroll the page in small increments to simulate human scrolling."""
    step_distance = distance // steps
    for _ in range(steps):
        await page.mouse.wheel(0, step_distance)
        await asyncio.sleep(random.uniform(0.1, 0.4))
