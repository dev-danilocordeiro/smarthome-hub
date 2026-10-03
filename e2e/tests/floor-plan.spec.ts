import { expect, test } from "@playwright/test";
import { openFirstHome, signIn } from "./support";

test("a member signs in, sees the live floor plan and switches a light through the device", async ({ page }) => {
  await signIn(page, "alice");
  await openFirstHome(page);

  const plan = page.getByRole("group", { name: "Floor plan" });
  const light = plan.getByRole("button", { name: /, Light: on|, Light: off/ }).first();
  await expect(light).toBeVisible();
  await light.click();

  const drawer = page.getByRole("complementary");
  const toggle = drawer.getByRole("button", { name: /^Turn (on|off)$/ });
  const before = await toggle.textContent();
  await toggle.click();
  // The label flips only once the device has applied the command and reported its new
  // state: API -> outbox -> MQTT -> simulated device -> ack -> live WebSocket.
  const after = before === "Turn on" ? "Turn off" : "Turn on";
  await expect(drawer.getByRole("button", { name: after })).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "Recent commands" })).toBeVisible();
  await expect(drawer.getByText("acknowledged").first()).toBeVisible();
});

test("someone who is not a member cannot open another person's home", async ({ page, browser }) => {
  await signIn(page, "alice");
  await openFirstHome(page);
  const url = page.url();

  const stranger = await browser.newPage();
  await signIn(stranger, "carol");
  await stranger.goto(url);
  await expect(stranger.getByRole("status").filter({ hasText: "No access" })).toBeVisible();
  await expect(stranger.getByRole("group", { name: "Floor plan" })).toHaveCount(0);
  await stranger.close();
});
