import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';
import { wordmarkHeaderTitle } from '../../../lib/Wordmark';

export default function SettingsStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen name="index" options={{ title: 'Settings', headerTitle: wordmarkHeaderTitle }} />
    </Stack>
  );
}
