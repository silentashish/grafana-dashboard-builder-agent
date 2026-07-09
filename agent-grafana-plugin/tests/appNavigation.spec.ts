import { test, expect } from './fixtures';
import { ROUTES } from '../src/constants';

test.describe('navigating app', () => {
  test('assistant should render successfully', async ({ gotoPage, page }) => {
    await gotoPage(`/${ROUTES.Assistant}`);
    await expect(page.getByTestId('data-testid assistant-container')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Assistant' })).toBeVisible();
    await expect(page.getByPlaceholder('Ask about telemetry, OpenSearch data, or Grafana dashboards')).toBeVisible();
  });
});
