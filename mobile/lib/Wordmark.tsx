// Official OnlyVibes wordmark (web/logo/wordmark.svg, rasterized) — used as
// the nav-bar heading on tabs and as in-page headings.
import { Image } from 'react-native';

const WORDMARK = require('../assets/brand/wordmark.png');

export function Wordmark({ height = 16 }: { height?: number }) {
  return (
    <Image
      source={WORDMARK}
      style={{ height, width: height * (372 / 80) }}
      resizeMode="contain"
    />
  );
}

/** headerTitle option rendering the wordmark in the nav bar */
export const wordmarkHeaderTitle = () => <Wordmark height={16} />;
