import { expect, test as base, type Page, type TestInfo } from "@playwright/test";

interface BrowserAudit {
  consoleErrors: string[];
  pageErrors: string[];
}

interface BrowserAuditFixtures {
  browserAudit: BrowserAudit;
}

const EXPECTED_HTTP_FAILURE_CONSOLE_MESSAGE = /^Failed to load resource: the server responded with a status of [45]\d\d/;

/** Fail UI journeys on browser runtime failures rather than only on assertions. */
export const test = base.extend<BrowserAuditFixtures>({
  browserAudit: [async ({ page }, use) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error" && !EXPECTED_HTTP_FAILURE_CONSOLE_MESSAGE.test(message.text())) consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await use({ consoleErrors, pageErrors });
    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  }, { auto: true }],
});

export { expect, type Page, type TestInfo };
