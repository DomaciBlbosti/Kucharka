/**
 * Průchodový test výpisu receptů v opravdovém prohlížeči.
 *
 * Proč vznikl: v CI má frontend jen `npm run build`, takže se ověří, že se
 * kód přeloží – ne že appka funguje. Tímhle sítem propadla chyba, kvůli
 * které se výpis receptů zacyklil na načítání: `params.getAll()` vrací
 * pokaždé nové pole, to bylo v závislostech useEffectu a React ho považoval
 * za změněné při každém renderu. Hlavní stránka zůstala prázdná a recepty
 * jen problikávaly. Build i lint byly přitom zelené.
 *
 * Test proto neověřuje vzhled, ale to, na čem appka stojí:
 *   – výpis se naplní a ZŮSTANE (žádná smyčka dotazů),
 *   – filtry a „Vařím z" fungují,
 *   – filtr přežije odchod na detail receptu a návrat,
 *   – v konzoli nic nespadne.
 *
 * Očekává běžící appku na BASE_URL (CI ji spouští před tímhle krokem).
 */
import { chromium } from "playwright";

const BASE = process.env.BASE_URL || "http://127.0.0.1:8099";
const SETTLE_MS = 2500;   // jak dlouho necháme případnou smyčku běžet
const QUIET_MS = 2000;    // okno, ve kterém už nesmí přijít žádný dotaz

let failed = 0;
const results = [];

function check(name, cond, detail = "") {
  results.push({ name, ok: !!cond, detail });
  if (!cond) failed++;
  console.log(`  ${cond ? "OK  " : "FAIL"} ${name}${cond || !detail ? "" : ` – ${detail}`}`);
}

/** Sleduje dotazy na API a chyby stránky. */
function watch(page) {
  const state = { calls: [], errors: [] };
  page.on("request", (r) => {
    const u = r.url();
    if (u.includes("/api/recipes")) state.calls.push({ url: u, at: Date.now() });
  });
  page.on("pageerror", (e) => state.errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") state.errors.push(m.text());
  });
  return state;
}

/** Klíčová kontrola: po ustálení už nesmí přijít ŽÁDNÝ další dotaz.
 *  Absolutní počet dotazů je křehký (debounce, StrictMode), ale „po chvíli
 *  je ticho" platí vždycky a smyčku odhalí spolehlivě. */
async function expectNoLoop(page, state, label) {
  await page.waitForTimeout(SETTLE_MS);
  const mark = Date.now();
  await page.waitForTimeout(QUIET_MS);
  const after = state.calls.filter((c) => c.at >= mark).length;
  check(`${label}: po ustálení už výpis nedotahuje (žádná smyčka)`, after === 0,
        `${after} dotazů za ${QUIET_MS} ms, celkem ${state.calls.length}`);
}

/** Otevře stránku a počká na obsah.
 *
 *  Schválně NE `waitUntil: "networkidle"`: kdyby se výpis zacyklil, síť by
 *  nikdy neztichla a test by spadl na timeoutu goto místo na srozumitelné
 *  kontrole „po ustálení už nedotahuje". Timeout je diagnóza k ničemu. */
async function open(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator('a[href^="/recept/"], a[href^="/varianty/"]')
    .first().waitFor({ timeout: 15000 })
    .catch(() => {});   // prázdný výpis řeší až kontrola níž, ne výjimka
}

async function recipeTitles(page) {
  return page.locator('a[href^="/recept/"], a[href^="/varianty/"]')
    .evaluateAll((els) => els.map((e) => e.innerText.split("\n")[0].trim())
                             .filter(Boolean));
}

async function main() {
  // V CI si Playwright stáhne prohlížeč sám (`playwright install chromium`).
  // CHROMIUM_PATH je pro stroje, kde už nějaký leží a nesedí verzí.
  const browser = await chromium.launch(
    process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
  );
  const page = await browser.newPage();
  const state = watch(page);

  // ── hlavní stránka ──
  console.log("\nhlavní stránka:");
  await open(page, BASE + "/");
  let titles = await recipeTitles(page);
  check("výpis receptů se naplní", titles.length >= 5, `${titles.length} karet`);
  check("nejsou to prázdné karty",
        titles.length > 0 && titles.every((t) => t.length > 1),
        JSON.stringify(titles.slice(0, 3)));
  await expectNoLoop(page, state, "hlavní stránka");
  check("konzole je bez chyb", state.errors.length === 0,
        state.errors.slice(0, 2).join(" | "));

  // ── filtr tagů ──
  console.log("\nfiltr tagů:");
  state.calls.length = 0;
  state.errors.length = 0;
  await page.getByRole("button", { name: /Tagy/ }).click();
  await page.getByRole("button", { name: "Hlavní jídlo" }).first().click();
  await page.waitForTimeout(800);
  check("výběr tagu se propíše do adresy",
        page.url().includes("tags=chod"), page.url());
  await expectNoLoop(page, state, "s filtrem tagu");
  titles = await recipeTitles(page);
  check("filtr něco vrátí (ne prázdno)", titles.length > 0, `${titles.length}`);
  check("konzole je bez chyb", state.errors.length === 0,
        state.errors.slice(0, 2).join(" | "));

  // ── filtr přežije odchod na detail a návrat ──
  // Tohle byla samostatná nahlášená chyba: filtry žily ve stavu komponenty,
  // takže se při návratu vyresetovaly.
  console.log("\nnávrat z detailu receptu:");
  const urlBefore = page.url();
  const detail = page.locator('a[href^="/recept/"]').first();
  if (await detail.count()) {
    await detail.click();
    await page.waitForURL(/\/recept\//);
    await page.goBack();
    await page.waitForTimeout(800);
    check("filtr po návratu zůstal", page.url() === urlBefore,
          `${page.url()} vs ${urlBefore}`);
    check("po návratu je zase vidět výpis",
          (await recipeTitles(page)).length > 0);
  } else {
    check("je na co kliknout (výpis dojel)", false, "žádný odkaz na recept");
  }

  // ── Vařím z ──
  console.log("\nVařím z:");
  state.calls.length = 0;
  state.errors.length = 0;
  await open(page, BASE + "/");
  await page.getByPlaceholder(/Přidat surovinu/).fill("rýže");
  await page.waitForTimeout(700);
  await page.getByRole("button", { name: /^rýže$/ }).first().click()
    .catch(async () => {
      // našeptávač může nabídnout položku jinou roli/značkou
      await page.locator("text=rýže").nth(1).click();
    });
  await page.waitForTimeout(1200);
  check("vybraná surovina je v adrese", /[?&]ing=/.test(page.url()), page.url());
  titles = await recipeTitles(page);
  check("„Vařím z“ vypíše recepty", titles.length > 0, `${titles.length} karet`);
  check("filtry zůstávají po ruce i tady",
        await page.getByRole("button", { name: /Tagy/ }).isVisible());
  await expectNoLoop(page, state, "Vařím z");
  check("konzole je bez chyb", state.errors.length === 0,
        state.errors.slice(0, 2).join(" | "));

  await browser.close();

  const ok = results.length - failed;
  console.log(`\n${ok} OK, ${failed} FAIL`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error("Průchodový test spadl:", e);
  process.exit(1);
});
