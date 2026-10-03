import { expect, type Page } from "@playwright/test";

export const PASSWORD = "smarthome-dev-1";

/** Sign in through the real Keycloak form, starting from the app like a person would. */
export async function signIn(page: Page, username: string): Promise<void> {
  await page.goto("/");
  await page.getByRole("link", { name: "Sign in" }).click();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(PASSWORD);
  await page.locator("#kc-login").click();
  await expect(page.getByRole("heading", { name: "Your homes" })).toBeVisible();
}

/** Open the first home in the list and wait for its live snapshot. */
export async function openFirstHome(page: Page): Promise<string> {
  const link = page.getByRole("listitem").getByRole("link").first();
  const name = (await link.textContent()) ?? "";
  await link.click();
  await expect(page.getByRole("status").filter({ hasText: "Live" })).toBeVisible();
  return name;
}
