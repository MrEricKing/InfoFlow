(() => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const INITIAL_DELAY_MS = 700;
  const STEP_DELAY_MS = 180;
  const PASS_SETTLE_DELAY_MS = 700;
  const COMMENT_SETTLE_DELAY_MS = 900;
  const FINAL_DELAY_MS = 900;
  const MAX_SCROLL_PASSES = 2;
  const MAX_SCROLL_STEPS_PER_PASS = 28;
  const MAX_COMMENT_SETTLE_MS = 12000;
  const MAX_PRECAPTURE_MS = 30000;

  const DYNAMIC_SECTION_SELECTORS = [
    "#comments",
    "#comment",
    "#remark42",
    "#waline",
    "#twikoo",
    "#cusdis_thread",
    "#cusdis",
    "#artalk",
    ".comments",
    ".comment",
    ".remark42",
    ".remark42__counter",
    ".giscus",
    ".utterances",
    ".waline",
    ".twikoo",
    ".cusdis",
    ".artalk",
    "[id*='comment']",
    "[class*='comment']",
    "[id*='remark']",
    "[class*='remark']",
    "[id*='giscus']",
    "[class*='giscus']",
    "[id*='utterance']",
    "[class*='utterance']",
    "[id*='waline']",
    "[class*='waline']",
    "[id*='twikoo']",
    "[class*='twikoo']",
    "[id*='cusdis']",
    "[class*='cusdis']",
    "[id*='artalk']",
    "[class*='artalk']",
    "iframe[src*='comment']",
    "iframe[src*='remark']",
    "iframe[src*='giscus']",
    "iframe[src*='utteranc']",
    "iframe[src*='waline']",
    "iframe[src*='twikoo']",
    "iframe[src*='cusdis']",
    "iframe[src*='artalk']",
  ];

  const LIGHT_THEME_STORAGE_VALUES = [
    ["theme", "light"],
    ["color-theme", "light"],
    ["colorScheme", "light"],
    ["color-scheme", "light"],
    ["appearance", "light"],
    ["prefers-color-scheme", "light"],
    ["chakra-ui-color-mode", "light"],
    ["theme-ui-color-mode", "light"],
    ["nuxt-color-mode", "light"],
    ["vueuse-color-scheme", "light"],
    ["darkMode", "false"],
    ["dark-mode", "false"],
  ];

  const setStorageValue = (storageName, key, value) => {
    try {
      const storage = window[storageName];
      storage.setItem(key, value);
    } catch (_error) {
      // Cross-origin frames and locked-down pages may reject storage access.
    }
  };

  const ensureColorSchemeMeta = () => {
    if (!document.head) {
      return;
    }
    let colorSchemeMeta = document.head.querySelector('meta[name="color-scheme"]');
    if (!colorSchemeMeta) {
      colorSchemeMeta = document.createElement("meta");
      colorSchemeMeta.setAttribute("name", "color-scheme");
      document.head.appendChild(colorSchemeMeta);
    }
    colorSchemeMeta.setAttribute("content", "light");
  };

  const applyLightThemeHints = () => {
    for (const [key, value] of LIGHT_THEME_STORAGE_VALUES) {
      setStorageValue("localStorage", key, value);
      setStorageValue("sessionStorage", key, value);
    }

    const root = document.documentElement;
    if (root) {
      root.style.colorScheme = "light";
      root.setAttribute("data-theme", "light");
      root.setAttribute("data-color-mode", "light");
      root.setAttribute("data-color-scheme", "light");
      root.classList.remove("dark", "theme-dark", "dark-mode");
      root.classList.add("light", "theme-light", "light-mode");
    }

    ensureColorSchemeMeta();
  };

  const triggerViewportEvents = () => {
    window.dispatchEvent(new Event("scroll", { bubbles: true }));
    window.dispatchEvent(new Event("resize", { bubbles: true }));
    document.dispatchEvent(new Event("scroll", { bubbles: true }));
  };

  const getPageHeight = () => {
    const bodyHeight = document.body ? document.body.scrollHeight : 0;
    const rootHeight = document.documentElement ? document.documentElement.scrollHeight : 0;
    return Math.max(bodyHeight, rootHeight);
  };

  const withinBudget = (startedAt) => Date.now() - startedAt <= MAX_PRECAPTURE_MS;

  const collectDynamicSections = () => {
    const sections = new Set();
    for (const selector of DYNAMIC_SECTION_SELECTORS) {
      try {
        for (const element of document.querySelectorAll(selector)) {
          sections.add(element);
        }
      } catch (_error) {
        // Ignore selector support differences across browser versions.
      }
    }
    return [...sections].filter((element) => {
      if (!element || typeof element.getBoundingClientRect !== "function") {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0 || element.tagName === "IFRAME";
    });
  };

  const buildDynamicSectionSignature = (sections) => {
    const pageHeight = getPageHeight();
    const sectionSummaries = sections.slice(0, 16).map((element) => {
      const rect = element.getBoundingClientRect();
      return [
        element.tagName,
        element.id || "",
        typeof element.className === "string" ? element.className : "",
        Math.round(rect.top + window.scrollY),
        Math.round(rect.height),
        element.childElementCount || 0,
        element.getAttribute("src") || "",
        (element.textContent || "").length,
      ].join(":");
    });
    return `${pageHeight}|${sectionSummaries.join("|")}`;
  };

  const waitForDynamicSections = async (startedAt) => {
    const deadline = Date.now() + MAX_COMMENT_SETTLE_MS;
    let sections = collectDynamicSections();
    if (!sections.length) {
      return;
    }

    for (const section of sections.slice(0, 6)) {
      if (!withinBudget(startedAt)) {
        return;
      }
      try {
        section.scrollIntoView({ block: "center", inline: "nearest" });
      } catch (_error) {
        const rect = section.getBoundingClientRect();
        window.scrollTo(0, Math.max(0, window.scrollY + rect.top - window.innerHeight / 2));
      }
      triggerViewportEvents();
      await sleep(COMMENT_SETTLE_DELAY_MS);
    }

    let stableCount = 0;
    let lastSignature = "";
    while (Date.now() < deadline && withinBudget(startedAt)) {
      sections = collectDynamicSections();
      const signature = buildDynamicSectionSignature(sections);
      if (signature === lastSignature) {
        stableCount += 1;
        if (stableCount >= 2) {
          break;
        }
      } else {
        stableCount = 0;
        lastSignature = signature;
      }
      window.scrollTo(0, Math.max(0, getPageHeight() - window.innerHeight));
      triggerViewportEvents();
      await sleep(COMMENT_SETTLE_DELAY_MS);
    }
  };

  const run = async () => {
    const startedAt = Date.now();
    await sleep(INITIAL_DELAY_MS);
    const step = Math.max(360, Math.floor(window.innerHeight * 0.75));
    let lastHeight = 0;

    for (let pass = 0; pass < MAX_SCROLL_PASSES; pass += 1) {
      const height = getPageHeight();
      let steps = 0;
      for (let y = 0; y <= height && steps < MAX_SCROLL_STEPS_PER_PASS; y += step) {
        if (!withinBudget(startedAt)) {
          break;
        }
        window.scrollTo(0, y);
        triggerViewportEvents();
        await sleep(STEP_DELAY_MS);
        steps += 1;
      }

      window.scrollTo(0, Math.max(0, height - window.innerHeight));
      triggerViewportEvents();
      await sleep(PASS_SETTLE_DELAY_MS);

      if (height === lastHeight || !withinBudget(startedAt)) {
        break;
      }
      lastHeight = height;
    }

    await waitForDynamicSections(startedAt);
    window.scrollTo(0, 0);
    triggerViewportEvents();
    await sleep(FINAL_DELAY_MS);
  };

  const startPreCapture = () => {
    if (
      !window.__infoflowSingleFilePreCapture ||
      typeof window.__infoflowSingleFilePreCapture.then !== "function"
    ) {
      window.__infoflowSingleFilePreCapture = run();
    }
    return window.__infoflowSingleFilePreCapture;
  };

  applyLightThemeHints();
  document.addEventListener("DOMContentLoaded", applyLightThemeHints, { once: true });
  dispatchEvent(new CustomEvent("single-file-user-script-init"));
  addEventListener("single-file-on-before-capture-request", (event) => {
    event.preventDefault();
    startPreCapture()
      .catch(() => undefined)
      .finally(() => {
        applyLightThemeHints();
        dispatchEvent(new CustomEvent("single-file-on-before-capture-response"));
      });
  });

  window.addEventListener(
    "load",
    () => {
      applyLightThemeHints();
      startPreCapture();
    },
    { once: true },
  );
})();
