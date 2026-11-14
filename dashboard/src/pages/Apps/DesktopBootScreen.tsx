import { useEffect, useState, type CSSProperties } from "react";
import { Bot, Cpu, Database, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useServerTimezone } from "../../hooks/useServerTimezone";
import { ExpertIcon } from "../Experts/components/iconForName";
import styles from "./index.module.less";

interface DesktopBootScreenProps {
  expertName: string;
  expertColor?: string | null;
  iconName?: string | null;
  iconUrl?: string | null;
  /** Always 4.5s in production; tests can shorten it. */
  durationMs?: number;
  onDone: () => void;
}

const BOOT_STEPS = [
  { key: "agent", fallback: "Agent runtime initialized" },
  { key: "desktops", fallback: "Mounting expert desktops" },
  { key: "workspace", fallback: "Linking workspace and memory" },
  { key: "pet", fallback: "Waking desktop companion" },
  { key: "prepare", fallback: "Preparing your workspace" },
] as const;

/**
 * "智管家" Agent OS login sequence. The visual language borrows the calm,
 * blurred-glass focus of a macOS login screen; the product mark is Octop's
 * two-lobed silhouette rendered in a white-to-ice-white gradient.
 */
export default function DesktopBootScreen({
  expertName,
  expertColor,
  iconName,
  iconUrl,
  durationMs = 4500,
  onDone,
}: DesktopBootScreenProps) {
  const { t, i18n } = useTranslation();
  const timeZone = useServerTimezone();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setTimeout(onDone, durationMs);
    return () => window.clearTimeout(timer);
  }, [durationMs, onDone]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const locale = i18n.language?.startsWith("zh") ? "zh-CN" : "en-US";
  const clockTime = new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: locale === "en-US",
    timeZone,
  }).format(now);
  const clockDate = new Intl.DateTimeFormat(locale, {
    month: "long",
    day: "numeric",
    weekday: "long",
    timeZone,
  }).format(now);

  return (
    <div
      className={styles.desktopBoot}
      data-testid="desktop-boot"
      role="status"
      aria-label={t("apps.desktop.boot.label", "Starting Agent OS")}
      style={
        {
          "--boot-accent": expertColor || "#7c8cff",
        } as CSSProperties
      }
    >
      <div className={styles.desktopBootBackdrop} aria-hidden>
        <span className={styles.desktopBootGrid} />
        <span className={styles.desktopBootScan} />
      </div>

      <div className={styles.desktopBootClock} aria-hidden>
        <span className={styles.desktopBootClockTime}>{clockTime}</span>
        <span className={styles.desktopBootClockDate}>{clockDate}</span>
      </div>

      <div className={styles.desktopBootOrbit} aria-hidden>
        <span className={styles.desktopBootRing} />
        <span className={styles.desktopBootRingInner} />
        <span className={styles.desktopBootNode}>
          <Bot size={14} />
        </span>
        <span className={styles.desktopBootNode}>
          <Cpu size={14} />
        </span>
        <span className={styles.desktopBootNode}>
          <Database size={14} />
        </span>
        <span className={styles.desktopBootNode}>
          <Sparkles size={14} />
        </span>
        <span className={styles.desktopBootCore} />
      </div>

      <div className={styles.desktopBootLogo}>
        <span className={styles.desktopBootIconWrap}>
          <span className={styles.desktopBootIconGlow} aria-hidden />
          <svg
            className={styles.desktopBootIcon}
            viewBox="6 14 52 36"
            aria-hidden
          >
            <defs>
              <linearGradient
                id="octop-white"
                x1="10"
                y1="20"
                x2="54"
                y2="44"
                gradientUnits="userSpaceOnUse"
              >
                <stop offset="0" stopColor="#ffffff" />
                <stop offset="0.55" stopColor="#f4f9ff" />
                <stop offset="1" stopColor="#dcecff" />
              </linearGradient>
              <linearGradient
                id="octop-core"
                x1="27"
                y1="27"
                x2="37"
                y2="37"
                gradientUnits="userSpaceOnUse"
              >
                <stop offset="0" stopColor="#eaf5ff" />
                <stop offset="1" stopColor="#c8e4ff" />
              </linearGradient>
            </defs>
            <path
              d="M32 26.2C26.6 22.7 20.6 20 16.2 20.5C12.4 20.9 10.3 23.5 10.3 27.2V36.8C10.3 40.5 12.4 43.1 16.2 43.5C20.6 44 26.6 41.3 32 37.8Z"
              fill="url(#octop-white)"
            />
            <path
              d="M32 26.2C37.4 22.7 43.4 20 47.8 20.5C51.6 20.9 53.7 23.5 53.7 27.2V36.8C53.7 40.5 51.6 43.1 47.8 43.5C43.4 44 37.4 41.3 32 37.8Z"
              fill="url(#octop-white)"
            />
            <rect
              x="26.8"
              y="26.8"
              width="10.4"
              height="10.4"
              rx="2.6"
              fill="url(#octop-core)"
              transform="rotate(45 32 32)"
            />
            <path
              d="M32 27.9C32.55 29.75 33.75 30.95 35.9 31.8C33.75 32.65 32.55 33.85 32 35.7C31.45 33.85 30.25 32.65 28.1 31.8C30.25 30.95 31.45 29.75 32 27.9Z"
              fill="#ffffff"
              fillOpacity="0.94"
            />
          </svg>
        </span>
        <span className={styles.desktopBootWordmark}>
          {t("apps.desktop.boot.brand", "智管家")}
        </span>
        <span className={styles.desktopBootTagline}>
          {t("apps.desktop.boot.tagline", "AGENT OS")}
        </span>
      </div>

      <div className={styles.desktopBootAgent}>
        <span className={styles.desktopBootAgentIcon}>
          <ExpertIcon iconName={iconName} iconUrl={iconUrl} size={24} />
        </span>
        <span className={styles.desktopBootAgentText}>
          <span className={styles.desktopBootAgentName}>{expertName}</span>
          <span className={styles.desktopBootAgentMeta}>
            {t("apps.desktop.boot.agentReady", "Agent runtime online")}
          </span>
        </span>
        <span className={styles.desktopBootAgentStatus} />
      </div>

      <span className={styles.desktopBootLogin}>
        <span className={styles.desktopBootLoginSpinner} aria-hidden />
        {t("apps.desktop.boot.signingIn", "Signing in to Agent OS")}
      </span>

      <div className={styles.desktopBootLog}>
        {BOOT_STEPS.map(({ key, fallback }) => (
          <span key={key} className={styles.desktopBootLogLine}>
            <span className={styles.desktopBootLogDot} />
            <span className={styles.desktopBootLogText}>
              {t(`apps.desktop.boot.${key}`, fallback)}
            </span>
          </span>
        ))}
        <span className={styles.desktopBootLogLine}>
          <span className={styles.desktopBootLogDot} />
          <span className={styles.desktopBootLogText}>
            {t("apps.desktop.boot.ready", "Desktop ready")}
          </span>
        </span>
      </div>

      <div className={styles.desktopBootPet} aria-hidden>
        <span className={styles.desktopBootPetBubble}>
          {t("apps.desktop.boot.petReady", "Desktop companion ready")}
        </span>
        <span className={styles.desktopBootPetMascot}>
          <span
            className={styles.desktopPetBody}
            style={
              { "--pet-accent": expertColor || "#7c8cff" } as CSSProperties
            }
          >
            <span className={styles.desktopPetEarLeft} />
            <span className={styles.desktopPetEarRight} />
            <span className={styles.desktopPetFace}>
              <span className={styles.desktopPetEyes}>
                <span className={styles.desktopPetEye} />
                <span className={styles.desktopPetEye} />
              </span>
              <span className={styles.desktopPetMouth} />
            </span>
          </span>
        </span>
      </div>

      <div className={styles.desktopBootMeta} aria-hidden>
        {t("apps.desktop.boot.meta", "AGENT / OS / DESKTOP")}
      </div>

      <div className={styles.desktopBootProgress} aria-hidden>
        <span className={styles.desktopBootProgressBar} />
      </div>

      <button type="button" className={styles.desktopBootSkip} onClick={onDone}>
        {t("apps.desktop.boot.skip", "Skip")}
      </button>
    </div>
  );
}
