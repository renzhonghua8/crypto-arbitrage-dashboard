// Tiny i18n system. Two languages, persisted in localStorage.
// Usage:
//   t("connected")                 -> "Connected" or "已连接"
//   <span data-i18n="title">        -> auto-replaced on language change
//   <input data-i18n-placeholder="search.placeholder">
window.I18N = (() => {
  const dict = {
    en: {
      title: "Crypto Arbitrage Live Dashboard",
      status_connecting: "Connecting…",
      status_connected: "Connected",
      status_disconnected: "Disconnected",
      updated_at: "Updated at",
      stats: (s) =>
        `${s.tickers_total} tickers (spot ${s.spots_total} / perp ${s.perps_total} / future ${s.futures_total})`,
      errors: (names) => `⚠ ${names} fetch failed`,
      search_placeholder: "Filter coin (e.g. PEPE)",
      min_volume_label: "Min 24h volume",
      vol_any: "Any",
      vol_100k: "100K",
      vol_1m: "1M",
      vol_10m: "10M",
      vol_100m: "100M",
      empty: "No opportunities match your filters",
      lang_toggle: "中文",

      tab_divergence: "⭐ Funding Divergence",
      tab_funding: "Funding (Single Ex)",
      tab_basis: "Spot–Futures Basis",
      tab_cross_perp: "Cross-Ex Spread (Perp)",
      tab_cross_spot: "Cross-Ex Spread (Spot)",

      hint_divergence: "Same coin, multiple exchanges. Go long on the cheapest-funding leg, short on the most-expensive leg — harvest funding on both sides. APR = APR(short leg) − APR(long leg).",
      hint_funding: "Spot long + perp short to harvest funding. APR = funding × (8760 / interval hours). Fees not deducted.",
      hint_basis: "Spot vs dated futures. APR = basis × 365 / days_to_expiry.",
      hint_cross_perp: "Cheapest perp ask vs richest perp bid across exchanges. Real execution cost includes fees + funding.",
      hint_cross_spot: "Cheapest spot ask vs richest spot bid. Withdrawal time and fees usually eat anything below 0.3%.",

      col_symbol: "Coin",
      col_long_ex: "Long Ex",
      col_long_rate: "Long Rate",
      col_short_ex: "Short Ex",
      col_short_rate: "Short Rate",
      col_interval: "Interval h",
      col_spread_8h: "Spread/8h",
      col_apr: "APR",
      col_min_volume: "Min vol $",
      col_exchange: "Exchange",
      col_spot: "Spot",
      col_perp: "Perp",
      col_future: "Future",
      col_basis_pct: "Basis %",
      col_funding_rate: "Funding",
      col_funding_apr: "APR",
      col_volume: "Volume $",
      col_days_to_expiry: "To Expiry",
      col_annualized: "APR",
      col_buy_ex: "Buy on",
      col_buy_price: "Buy price",
      col_sell_ex: "Sell on",
      col_sell_price: "Sell price",
      col_spread: "Spread %",
    },
    zh: {
      title: "加密套利实时面板",
      status_connecting: "连接中…",
      status_connected: "已连接",
      status_disconnected: "未连接",
      updated_at: "更新于",
      stats: (s) =>
        `共 ${s.tickers_total} tickers (现货 ${s.spots_total} / 永续 ${s.perps_total} / 交割 ${s.futures_total})`,
      errors: (names) => `⚠ ${names} 拉取失败`,
      search_placeholder: "过滤币种 (例: PEPE)",
      min_volume_label: "最低日成交额",
      vol_any: "不限",
      vol_100k: "10万",
      vol_1m: "100万",
      vol_10m: "1000万",
      vol_100m: "1亿",
      empty: "没有符合过滤条件的机会",
      lang_toggle: "EN",

      tab_divergence: "⭐ 资金费率跨所分化",
      tab_funding: "资金费率（单所）",
      tab_basis: "期现基差",
      tab_cross_perp: "跨所价差（永续）",
      tab_cross_spot: "跨所价差（现货）",

      hint_divergence: "同一币种在多个交易所的资金费率分化。A 所做多 + B 所做空，双边吃 funding。年化 = (短腿 - 长腿) APR 之和。",
      hint_funding: "现货 + 永续空单，吃资金费率。年化 = funding × (8760 / 间隔)。未扣手续费。",
      hint_basis: "现货 vs 交割合约。年化 = basis × 365 / 距到期天数。",
      hint_cross_perp: "同一永续在不同所的最便宜卖一 vs 最贵买一。实际可执行价差需扣手续费 + 资金成本。",
      hint_cross_spot: "同一现货在不同所的价差。提币时间/费用经常吃掉这点价差，看到 0.3% 以下基本没机会。",

      col_symbol: "币种",
      col_long_ex: "做多所",
      col_long_rate: "多腿费率",
      col_short_ex: "做空所",
      col_short_rate: "空腿费率",
      col_interval: "间隔h",
      col_spread_8h: "8h价差%",
      col_apr: "年化%",
      col_min_volume: "最小成交$",
      col_exchange: "交易所",
      col_spot: "现货",
      col_perp: "永续",
      col_future: "期货",
      col_basis_pct: "基差%",
      col_funding_rate: "本期费率",
      col_funding_apr: "年化%",
      col_volume: "成交$",
      col_days_to_expiry: "距到期",
      col_annualized: "年化%",
      col_buy_ex: "买入所",
      col_buy_price: "买入价",
      col_sell_ex: "卖出所",
      col_sell_price: "卖出价",
      col_spread: "价差%",
    },
  };

  const STORAGE_KEY = "arb_lang";
  let current = localStorage.getItem(STORAGE_KEY);
  if (current !== "en" && current !== "zh") {
    current = (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  function t(key) {
    return (dict[current] && dict[current][key]) ?? key;
  }

  function setLang(lang) {
    if (!dict[lang]) return;
    current = lang;
    localStorage.setItem(STORAGE_KEY, lang);
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    applyDom();
    if (typeof window.onLanguageChange === "function") window.onLanguageChange();
  }

  function getLang() {
    return current;
  }

  function applyDom() {
    document.title = t("title");
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
  }

  document.addEventListener("DOMContentLoaded", applyDom);

  return { t, setLang, getLang, applyDom };
})();
