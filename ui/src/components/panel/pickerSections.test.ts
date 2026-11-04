import { describe, expect, it } from 'vitest';
import { clubSections, initialPickerSection, pickerGridRows, trainingImplementSections } from './pickerSections';

describe('initialPickerSection', () => {
  const clubs = clubSections();

  it('opens Woods when the driver is selected', () => {
    expect(initialPickerSection(clubs, 'driver')).toBe('Woods');
  });

  it('opens Irons when a wedge is selected', () => {
    expect(initialPickerSection(clubs, 'pw')).toBe('Irons');
  });

  it('falls back to the first section when the id is unknown', () => {
    expect(initialPickerSection(clubs, 'not-a-club')).toBe('Wedges');
  });

  it('opens the matching training group', () => {
    const groups = trainingImplementSections();
    expect(initialPickerSection(groups, 'stack-160g')).toBe('TheStack');
  });
});

describe('clubSections', () => {
  it('orders the club families from short to long while keeping the selected family', () => {
    expect(clubSections().map(section => section.name)).toEqual(['Wedges', 'Irons', 'Hybrids', 'Woods']);
    const sections = clubSections(['driver', '7-iron', 'wedge-58']);
    expect(sections.map(section => section.name)).toEqual(['Wedges', 'Irons', 'Woods']);
    expect(initialPickerSection(sections, '7-iron')).toBe('Irons');
    expect(initialPickerSection(sections, 'driver')).toBe('Woods');
  });
});

describe('pickerGridRows', () => {
  it('uses three rows for the iron set so woods tiles match that cube size', () => {
    expect(pickerGridRows(clubSections())).toBe(3);
  });

  it('uses the densest training group so every tab fits', () => {
    expect(pickerGridRows(trainingImplementSections())).toBe(4);
  });
});
