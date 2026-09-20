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
            <span className="tag" style={{ marginLeft: "auto" }} title="This build places no orders in any mode.">
              READ-ONLY
            </span>
          </header>
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
