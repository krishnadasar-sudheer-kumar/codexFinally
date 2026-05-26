import { expect, test } from '@playwright/test';

async function waitForApp(page: import('@playwright/test').Page) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await page.goto('/', { waitUntil: 'domcontentloaded', timeout: 3_000 });
      if (response?.ok()) {
        return response;
      }
    } catch {
      await page.waitForTimeout(1_000);
    }
  }

  return page.goto('/', { waitUntil: 'domcontentloaded' });
}

async function openWorkstation(page: import('@playwright/test').Page) {
  const response = await waitForApp(page);
  expect(response?.ok(), 'root route should return a successful response').toBe(true);
  await expect(page.getByRole('heading', { name: 'Trading Workstation' })).toBeVisible();
}

test.describe('FinAlly application shell', () => {
  test('fresh start serves an application page', async ({ page }) => {
    const response = await waitForApp(page);

    expect(response?.ok(), 'root route should return a successful response').toBe(true);
    await expect(page.locator('body')).toBeVisible();
    await expect(page.locator('body')).not.toContainText(/traceback|internal server error/i);
  });
});

test.describe.serial('FinAlly integrated workflows', () => {
  test('fresh start shows default watchlist, cash balance, and streaming prices', async ({ page }) => {
    await openWorkstation(page);

    await expect(page.getByTestId('metric-cash')).toContainText('$');
    await expect(page.getByTestId('watch-row-AAPL')).toBeVisible();
    await expect(page.getByText(/connected|reconnecting/i)).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Trading Workstation' })).toBeVisible();
  });

  test('watchlist supports adding and removing tickers with guardrail feedback', async ({ page, request }) => {
    await request.delete('/api/watchlist/IBM');
    await openWorkstation(page);

    await page.getByLabel('Add ticker').fill('IBM');
    await page.getByRole('button', { name: 'Add' }).click();

    await expect(page.getByTestId('notice')).toContainText(/IBM (added|is already)/);
    await expect(page.getByTestId('watch-row-IBM')).toBeVisible();

    await page.getByLabel('Remove IBM').click();

    await expect(page.getByTestId('notice')).toContainText('IBM removed');
    await expect(page.getByTestId('watch-row-IBM')).toHaveCount(0);
  });

  test('trade flow supports buy, sell, and fractional-share market orders', async ({ page }) => {
    await openWorkstation(page);

    await page.getByLabel('Trade ticker').fill('AAPL');
    await page.getByLabel('Trade quantity').fill('1.25');
    await page.getByRole('button', { name: 'Buy' }).click();

    await expect(page.getByTestId('notice')).toContainText('BUY 1.25 AAPL submitted.');
    await expect(page.getByTestId('position-row-AAPL')).toBeVisible();

    await page.getByLabel('Trade quantity').fill('0.25');
    await page.getByRole('button', { name: 'Sell' }).click();

    await expect(page.getByTestId('notice')).toContainText('SELL 0.25 AAPL submitted.');
    await expect(page.getByTestId('position-row-AAPL')).toBeVisible();
  });

  test('mocked chat can respond, execute valid trades, and report invalid mutations', async ({ page }) => {
    await openWorkstation(page);

    await page.getByLabel('Assistant message').fill('Please buy 1 share of MSFT');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect(page.getByTestId('chat-log')).toContainText('Mock mode: prepared a simulated buy order');
    await expect(page.getByTestId('chat-log')).toContainText('1 trade action(s)');
    await expect(page.getByTestId('position-row-MSFT')).toBeVisible();

    await page.getByLabel('Assistant message').fill('Please sell 9999999 shares of MSFT');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect(page.getByTestId('chat-log')).toContainText(/order_value_exceeded|insufficient_shares/);
    await expect(page.locator('body')).not.toContainText(/objects are not valid as a react child/i);
  });
});
