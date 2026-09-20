"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * Primary navigation.
 *
 * Sections that do not exist yet are rendered as disabled items tagged
 * PLANNED, not as links to empty pages. Showing them keeps the roadmap honest
 * and visible; linking them would be a promise the build cannot keep.
 */

const BUILT = [
  { href: "/markets", label: "Markets" },
  { href: "/scanner", label: "Scanner" },
] as const;

/** Phase in which each section is scheduled — shown so the gap is explicit. */
const PLANNED = [
  { label: "Paper Trading", phase: 6 },
  { label: "Backtest", phase: 5 },
  { label: "Falcon", phase: 9 },
  { label: "Watchlist", phase: 3 },
  { label: "Account", phase: 1 },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="nav" aria-label="Primary">
      {BUILT.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={pathname.startsWith(item.href) ? "page" : undefined}
        >
          {item.label}
        </Link>
      ))}
      {PLANNED.map((item) => (
        <span
          key={item.label}
          className="disabled"
          aria-disabled="true"
          title={`Not implemented. Scheduled for phase ${item.phase}.`}
        >
          {item.label}
          <span className="tag">PLANNED</span>
        </span>
      ))}
    </nav>
  );
}
