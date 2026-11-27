import type { ReactNode } from "react";
import Link from "next/link";
import PrimaryNavigation from "@/components/PrimaryNavigation";
import "./globals.css";

export const metadata = {
  title: "JIRAN CODING — LLM-Assisted Development",
  description: "LLM-assisted development workspace with self-healing ITSM.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="appHeader">
          <Link className="brand" href="/">
            <span className="brandMark">J</span>
            <span>
              <strong>JIRAN CODING</strong>
              <small>LLM-Assisted Development</small>
            </span>
          </Link>
          <PrimaryNavigation />
          <span className="localOnlyBadge">LOCAL ONLY</span>
        </header>
        <main className="mainContent">{children}</main>
      </body>
    </html>
  );
}
