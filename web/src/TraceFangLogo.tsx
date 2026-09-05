import traceFangLogo from "./assets/tracefang-logo.png";

interface TraceFangLogoProps {
  className?: string;
}

export function TraceFangLogo({ className }: TraceFangLogoProps) {
  return (
    <img
      className={["tracefang-logo", className].filter(Boolean).join(" ")}
      src={traceFangLogo}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}
