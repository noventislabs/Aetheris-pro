import type { Metadata } from "next";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Aetheris Pro Terminal",
  description: "Read-only market intelligence terminal for USDT-M perpetuals",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0a0d12",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="topbar">
            <div className="brand">
              AETHERIS<span> PRO</span>
            </div>
            <Nav />
            {/*
              Was READ-ONLY through phase 5. Paper trading writes -- to
              simulation state -- so that claim stopped being true, and a badge
              that overstates the guarantee is worse than one that states the
              real one precisely.
            */}
            <span
              className="tag"
              style={{ marginLeft: "auto" }}
              title="Paper trading simulates against real prices. No order is sent to any exchange in any mode, no API credential exists, and testnet and live execution are not built."
            >
              NO REAL ORDERS
            </span>
          </header>
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
