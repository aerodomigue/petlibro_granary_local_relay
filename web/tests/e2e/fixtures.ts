import { expect, test as base, type Page, type TestInfo } from "@playwright/test";

interface BrowserAudit {
  consoleErrors: string[];
  pageErrors: string[];
}

interface BrowserAuditFixtures {
  browserAudit: BrowserAudit;
}

/** Fail UI journeys on browser runtime failures rather than only on assertions. */
export const test = base.extend<BrowserAuditFixtures>({
  browserAudit: async ({ page }, use) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await use({ consoleErrors, pageErrors });
    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  },
});

export { expect, type Page, type TestInfo };
