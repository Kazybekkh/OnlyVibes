import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';

export default function SettingsStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen name="index" options={{ title: 'Settings', headerTitle: 'Settings' }} />
    </Stack>
  );
}
