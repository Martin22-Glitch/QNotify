// 内联线性图标（24 栅格，1.8 描边）。不引入图标库依赖。
import type { ReactNode, SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function base(paths: ReactNode) {
  return function Icon({ size = 20, ...rest }: P) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        {...rest}
      >
        {paths}
      </svg>
    );
  };
}

export const IconCheck = base(<path d="M5 12.5l4.5 4.5L19 7.5" />);
export const IconTodo = base(
  <>
    <rect x="3.5" y="3.5" width="17" height="17" rx="5" />
    <path d="M8 12.2l2.8 2.8L16.2 9.6" />
  </>,
);
export const IconInbox = base(
  <>
    <path d="M4 13.5l2.2-7.2A2 2 0 0 1 8.1 5h7.8a2 2 0 0 1 1.9 1.3L20 13.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
    <path d="M4 13.5h4.5l1.2 2h4.6l1.2-2H20" />
  </>,
);
export const IconChat = base(
  <path d="M5 18.5V7a2.5 2.5 0 0 1 2.5-2.5h9A2.5 2.5 0 0 1 19 7v6.5a2.5 2.5 0 0 1-2.5 2.5H9l-4 2.5z" />,
);
export const IconSpark = base(
  <>
    <path d="M12 3.5l1.8 4.9 4.9 1.8-4.9 1.8L12 16.9l-1.8-4.9L5.3 10.2l4.9-1.8z" />
    <path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" />
  </>,
);
export const IconGear = base(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V20a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H4a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H10a1.6 1.6 0 0 0 1-1.5V4a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V10a1.6 1.6 0 0 0 1.5 1H20a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" />
  </>,
);
export const IconCal = base(
  <>
    <rect x="3.5" y="5" width="17" height="15" rx="3" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </>,
);
export const IconLeft = base(<path d="M14.5 6l-6 6 6 6" />);
export const IconRight = base(<path d="M9.5 6l6 6-6 6" />);
export const IconDown = base(<path d="M6 9.5l6 6 6-6" />);
export const IconX = base(<path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />);
export const IconHelp = base(
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M9.8 9.6a2.3 2.3 0 0 1 4.4.9c0 1.6-2.2 2-2.2 3.3M12 16.6v.1" />
  </>,
);
export const IconSearch = base(
  <>
    <circle cx="11" cy="11" r="6" />
    <path d="M20 20l-4.6-4.6" />
  </>,
);
export const IconEyeOff = base(
  <>
    <path d="M4 4l16 16" />
    <path d="M10.6 6.1A9.6 9.6 0 0 1 12 6c5 0 8.5 4.5 9 6-.3.8-1.2 2.3-2.7 3.6M6.4 7.6C4.6 8.9 3.4 10.8 3 12c.5 1.5 4 6 9 6 1.4 0 2.6-.3 3.7-.9" />
    <path d="M9.9 10a3 3 0 0 0 4.1 4.1" />
  </>,
);
export const IconUndo = base(
  <>
    <path d="M9 7L4.5 11.5 9 16" />
    <path d="M5 11.5h9.5a5 5 0 0 1 0 10H12" />
  </>,
);
export const IconClock = base(
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </>,
);
export const IconUsers = base(
  <>
    <circle cx="9" cy="8.5" r="3.2" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
    <path d="M15.5 5.6a3.2 3.2 0 0 1 0 5.8M17.5 14a5.5 5.5 0 0 1 3 5" />
  </>,
);
export const IconSend = base(<path d="M4.5 12L20 4.5 15.5 20l-3-6.5z M12.5 13.5L20 4.5" />);
export const IconRefresh = base(
  <>
    <path d="M19.5 12a7.5 7.5 0 0 1-13.1 5" />
    <path d="M4.5 12a7.5 7.5 0 0 1 13.1-5" />
    <path d="M17.8 3.5v3.8H14M6.2 20.5v-3.8H10" />
  </>,
);
export const IconEdit = base(
  <>
    <path d="M4.5 19.5l1-4L15.8 5.2a2 2 0 0 1 2.9 0l.1.1a2 2 0 0 1 0 2.9L8.5 18.5z" />
    <path d="M13.5 7.5l3 3" />
  </>,
);
export const IconArrow = base(<path d="M5 12h14M13 6l6 6-6 6" />);
export const IconQuote = base(
  <path d="M7 17c-1.7 0-3-1.3-3-3 0-3.3 1.8-6.1 4.5-7.5M16 17c-1.7 0-3-1.3-3-3 0-3.3 1.8-6.1 4.5-7.5M4.5 13H9v4H4.5zM13.5 13H18v4h-4.5z" />,
);
export const IconUser = base(
  <>
    <circle cx="12" cy="8.5" r="3.5" />
    <path d="M5 20a7 7 0 0 1 14 0" />
  </>,
);
export const IconPlug = base(
  <>
    <path d="M9 3.5v4M15 3.5v4M6.5 7.5h11V11a5.5 5.5 0 0 1-11 0z" />
    <path d="M12 16.5v4" />
  </>,
);
export const IconWifiOff = base(
  <>
    <path d="M3.5 3.5l17 17" />
    <path d="M8.5 16.2a5 5 0 0 1 7 0M5.2 12.8a9.6 9.6 0 0 1 4-2.4M15.5 10.6a9.6 9.6 0 0 1 3.3 2.2M2 9.4a14 14 0 0 1 4.2-2.7M11 5.6a14 14 0 0 1 11 3.8" />
    <circle cx="12" cy="19.5" r=".6" fill="currentColor" />
  </>,
);
export const IconBolt = base(<path d="M13 3L5 13.5h6L10.5 21 19 10.5h-6z" />);
