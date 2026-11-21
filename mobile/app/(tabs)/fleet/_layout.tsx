// Fleet stack: transparent, blurred nav bar; the nav heading is the official
// OnlyVibes wordmark (web/logo/wordmark.svg), not text.
import { Stack } from 'expo-router';
import { Image } from 'react-native';
import { headerOptions } from '../../../lib/headerOptions';

const WORDMARK = require('../../../assets/brand/wordmark.png');

export default function FleetStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen
        name="index"
        options={{
          title: 'OnlyVibes',
          headerTitle: () => (
            <Image source={WORDMARK} style={{ width: 74, height: 16 }} resizeMode="contain" />
          ),
        }}
      />
    </Stack>
  );
}
