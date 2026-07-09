import { test, expect } from './fixtures';

test('should be possible to save app configuration', async ({ appConfigPage, page }) => {
  const saveButton = page.getByRole('button', { name: /Save assistant settings/i });

  // reset the configured secret
  await page.getByRole('button', { name: /reset/i }).click();

  // enter some valid values
  await page.getByRole('textbox', { name: 'API key' }).fill('secret-api-key');
  await page.getByRole('textbox', { name: 'HTTP URL' }).clear();
  await page.getByRole('textbox', { name: 'HTTP URL' }).fill('http://host.docker.internal:8000');
  await page.getByRole('textbox', { name: 'WebSocket URL' }).fill('ws://localhost:8000/ws/assistant');

  // listen for the server response on the saved form
  const saveResponse = appConfigPage.waitForSettingsResponse();

  await saveButton.click();
  await expect(saveResponse).toBeOK();
});
