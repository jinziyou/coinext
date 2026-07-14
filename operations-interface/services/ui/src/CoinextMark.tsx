// coinext 品牌字形 —— 字母「C」收成一枚开口币环 + 向前的「›」(next/上行信号)。
// C 用 currentColor（继承周围文字色，深浅皆宜），「›」为电青→量子青渐变。
export function CoinextMark({ className }: { className?: string }) {
  const gid = "cx-mark-grad";
  return (
    <svg
      viewBox="0 0 64 64"
      className={className}
      role="img"
      aria-label="coinext"
      fill="none"
    >
      <defs>
        <linearGradient id={gid} x1="31" y1="46" x2="49" y2="18" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#12b5a5" />
          <stop offset="1" stopColor="#22d3ee" />
        </linearGradient>
      </defs>
      <path
        d="M41.6 14.6 A20 20 0 1 0 41.6 49.4"
        stroke="currentColor"
        strokeWidth="7"
        strokeLinecap="round"
      />
      <path
        d="M33 20 L48 32 L33 44"
        stroke={`url(#${gid})`}
        strokeWidth="7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
