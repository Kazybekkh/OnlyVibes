// Fleet stack: no nav bar — the official OnlyVibes wordmark is the in-page
// heading on the Fleet screen itself (single title, no duplication).
import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';

export default function FleetStack() {
  return (
    <Stack screenOptions={{ ...headerOptions, headerShown: false }}>
      <Stack.Screen name="index" options={{ title: 'OnlyVibes' }} />
    </Stack>
  );
}
