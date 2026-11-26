import type { ReactNode } from "react";
import Link from "next/link";
import PrimaryNavigation from "@/components/PrimaryNavigation";
import "./globals.css";
import { PROMETHEUS_URL } from "@/lib/prometheus";

export const metadata = {
  title: "ITSM — Ticket Operations",
  description: "Workspace tiket ITSM dengan integrasi coding agent lokal.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="id">
      <body>
        <header className="appHeader">
          <Link className="brand" href="/">
            <span className="brandMark">J</span>
            <span>
              <strong>JIRAN ITSM</strong>
              <small>Ticket operations</small>
            </span>
          </Link>
          <PrimaryNavigation />
          <span className="localOnlyBadge">LOCAL ONLY</span>
        </header>
        <main className="mainContent">{children}</main>
        <footer className="appFooter">
          ITSM server · Prometheus: {PROMETHEUS_URL}
        </footer>
      </body>
    </html>
  );
}
