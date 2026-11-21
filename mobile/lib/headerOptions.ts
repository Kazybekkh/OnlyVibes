// Shared native-stack header: on iOS a transparent, blurred bar with a
// collapsing large title (ScrollViews use contentInsetAdjustmentBehavior to
// clear it). Android/web have no automatic content inset, so the header is
// opaque there — otherwise the title draws on top of the first card.
import type { Stack } from 'expo-router';
import type { ComponentProps } from 'react';
import { Platform } from 'react-native';
import { colors } from './theme';

type StackOptions = Exclude<NonNullable<ComponentProps<typeof Stack>['screenOptions']>, (...args: never[]) => unknown>;

const ios = Platform.OS === 'ios';

export const headerOptions: StackOptions = {
  headerShown: true,
  headerTransparent: ios,
  headerLargeTitle: ios,
  headerLargeTitleShadowVisible: false,
  headerShadowVisible: false,
  headerTintColor: colors.label,
  headerTitleStyle: { fontWeight: '700', color: colors.label },
  headerLargeTitleStyle: { color: colors.label },
  headerBlurEffect: 'systemUltraThinMaterial',
  headerStyle: { backgroundColor: ios ? 'transparent' : colors.bg },
  contentStyle: { backgroundColor: colors.bg },
};
