"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getSymbols } from "@/lib/api";
import type { SymbolInfo } from "@/lib/types";

/**
 * Symbol picker over the dynamically discovered universe.
 *
 * The list always comes from the backend's search endpoint, so it reflects
 * whatever the venue currently lists — there is no bundled symbol list to drift
 * out of date.
 *
 * Keyboard: `/` focuses from anywhere, arrows move, Enter selects, Escape
 * closes. A trading terminal that needs the mouse to change instrument is a
 * trading terminal nobody uses.
 */

const DEBOUNCE_MS = 220;

export function SymbolSearch({
  value,
  onSelect,
}: {
  value: string;
  onSelect: (symbol: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SymbolInfo[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [failed, setFailed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Global "/" shortcut.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/") return;
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable;
      if (typing) return;
      event.preventDefault();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Close when focus or a click leaves the widget.
  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, []);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const page = await getSymbols(query.trim() || null, controller.signal);
        setResults(page.symbols);
        setActive(0);
        setFailed(false);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // The dropdown reports that it could not load rather than showing an
        // empty list, which would read as "no such symbol".
        setResults([]);
        setFailed(true);
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, open]);

  const choose = useCallback(
    (symbol: string) => {
      onSelect(symbol);
      setOpen(false);
      setQuery("");
      inputRef.current?.blur();
    },
    [onSelect],
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      setOpen(false);
      inputRef.current?.blur();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, Math.max(results.length - 1, 0)));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const picked = results[active];
      if (picked) choose(picked.symbol);
    }
  };

  return (
    <div className="search" ref={containerRef}>
      <input
        ref={inputRef}
        className="input"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls="symbol-results"
        aria-autocomplete="list"
        placeholder={`Search instruments (${value})`}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        aria-label="Search instruments"
      />
      {!open ? <span className="search-hint">/</span> : null}

      {open ? (
        <ul className="search-results" id="symbol-results" role="listbox">
          {failed ? (
            <li>
              <span style={{ display: "block", padding: "6px 8px", color: "var(--down)" }}>
                Could not load instruments
              </span>
            </li>
          ) : results.length === 0 ? (
            <li>
              <span style={{ display: "block", padding: "6px 8px", color: "var(--text-faint)" }}>
                {query.trim() ? `No instrument matches "${query.trim()}"` : "No instruments"}
              </span>
            </li>
          ) : (
            results.map((symbol, index) => (
              <li key={symbol.symbol} role="option" aria-selected={index === active}>
                <button
                  type="button"
                  data-active={index === active}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => choose(symbol.symbol)}
                >
                  <span>{symbol.symbol}</span>
                  <span style={{ color: "var(--text-faint)" }}>{symbol.base_asset}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}
