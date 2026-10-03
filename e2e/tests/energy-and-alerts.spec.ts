import { expect, test } from "@playwright/test";
import { openFirstHome, signIn } from "./support";

test("the energy tab reports consumption and saves a tariff", async ({ page }) => {
  await signIn(page, "alice");
  await openFirstHome(page);
  await page.getByRole("link", { name: "Energy" }).click();

  await expect(page.getByRole("heading", { name: "Energy" })).toBeVisible();
  await expect(page.getByText(/kWh/).first()).toBeVisible();
  await page.getByRole("combobox", { name: /^Period/ }).selectOption("24h");
  await expect(page.getByText(/whole-home meter|sum of smart plugs/)).toBeVisible();

  const price = page.getByLabel(/Price per kWh/);
  await price.fill("0.812345");
  await page.getByRole("button", { name: "Save tariff" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel(/Price per kWh/)).toHaveValue("0.812345");
});

test("alerts, notification preferences and the inbox are reachable", async ({ page }) => {
  await signIn(page, "alice");
  await openFirstHome(page);
  await page.getByRole("link", { name: "Alerts" }).click();

  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "My notifications" })).toBeVisible();
  await page.getByRole("button", { name: "Save" }).first().click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." }).first()).toBeVisible();

  await page.getByRole("button", { name: /^Notifications, \d+ unread$/ }).click();
  await expect(page.getByRole("dialog", { name: "Notifications" })).toBeVisible();
});

test("signing out ends the session", async ({ page }) => {
  await signIn(page, "alice");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  const session = await page.request.get("/api/auth/session");
  expect(session.status()).toBe(401);
});
