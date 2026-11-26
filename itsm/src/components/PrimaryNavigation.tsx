"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/agent", label: "Agent" },
  { href: "/", label: "Tickets" },
  { href: "/monitoring", label: "Monitoring" },
];

export default function PrimaryNavigation() {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary navigation">
      {links.map(({ href, label }) => {
        const active =
          pathname === href || (href !== "/" && pathname.startsWith(`${href}/`));
        return (
          <Link
            aria-current={active ? "page" : undefined}
            className={active ? "activeNavLink" : undefined}
            href={href}
            key={href}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
