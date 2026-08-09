import {
  customSeriesDefaultOptions,
  type CustomData,
  type CustomSeriesOptions,
  type ICustomSeriesPaneRenderer,
  type ICustomSeriesPaneView,
  type ISeriesApi,
  type PaneRendererCustomData,
  type SeriesPartialOptions,
  type Time,
  type WhitespaceData,
} from "lightweight-charts";

import {
  TIMELINE_CLOCK_BUCKET_SECONDS,
  buildTimelineAxisAnchors,
  buildTimelineLogicalDayRanges,
  tradingDayAt,
  type ProjectedTimelinePoint,
  type TimelineLayout,
  type TimelineLogicalDayRange,
} from "./chartTimeAxis.ts";
import type { MarketSchedule } from "./types.ts";

export interface TimelineClockSample {
  actualTime: number;
  chartTime: number;
  value: number;
  segment: number;
  sourceIndex: number;
  offsetSeconds: number;
}

export interface TimelineClockBucket extends CustomData<Time> {
  time: Time;
  samples: TimelineClockSample[];
  high: number;
  low: number;
  last: number;
}

export type TimelineClockSeriesData = TimelineClockBucket | WhitespaceData<Time>;

export interface TimelineClockDomain {
  data: TimelineClockSeriesData[];
  ranges: TimelineLogicalDayRange[];
  pointBucketTimes: number[];
  pointDataIndexes: number[];
  rangeDataEnds: number[];
  sourcePoints: readonly ProjectedTimelinePoint[];
  visiblePointCount: number;
}

export interface TimelineClockSeriesOptions extends CustomSeriesOptions {
  lineColor: string;
  lineWidth: number;
  topColor: string;
  bottomColor: string;
}

export type TimelineClockSeriesApi = ISeriesApi<
  "Custom",
  Time,
  TimelineClockSeriesData,
  TimelineClockSeriesOptions,
  SeriesPartialOptions<TimelineClockSeriesOptions>
>;

interface MutableBucket {
  time: Time;
  samples: TimelineClockSample[];
  high: number;
  low: number;
  last: number;
}

type DrawTarget = Parameters<ICustomSeriesPaneRenderer["draw"]>[0];
type PriceConverter = Parameters<ICustomSeriesPaneRenderer["draw"]>[1];

function normalizedPointCount(points: readonly unknown[], requestedPointCount: number): number {
  const requested = Number.isFinite(requestedPointCount)
    ? Math.max(0, Math.floor(requestedPointCount))
    : 0;
  return Math.min(points.length, requested);
}

export function recentTimelineWindowStart(
  points: readonly { actualTime: number }[],
  requestedDayCount: number,
  schedule: MarketSchedule | null | undefined,
): number | null {
  let index = points.length - 1;
  let remainingDays = Math.max(
    1,
    Math.floor(Number.isFinite(requestedDayCount) ? requestedDayCount : 1),
  );
  let oldestStart: number | null = null;
  while (index >= 0 && remainingDays > 0) {
    const actualTime = points[index]?.actualTime;
    const day = Number.isFinite(actualTime) ? tradingDayAt(actualTime, schedule) : null;
    if (!day) {
      index -= 1;
      continue;
    }
    oldestStart = day.actualStart;
    remainingDays -= 1;
    let low = 0;
    let high = index + 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (points[middle].actualTime < day.actualStart) low = middle + 1;
      else high = middle;
    }
    index = low - 1;
  }
  return oldestStart;
}

export function timelineClockBucketTimesAreAdjacent(
  previousBucketTime: number,
  nextBucketTime: number,
): boolean {
  const elapsed = nextBucketTime - previousBucketTime;
  return elapsed >= 0
    && elapsed <= TIMELINE_CLOCK_BUCKET_SECONDS + Number.EPSILON;
}

/**
 * Builds a density-independent clock domain without manufacturing a price.
 * Empty minutes are whitespace only; every real observation remains inside
 * its minute bucket and is rendered at its exact fractional clock position.
 */
export function buildTimelineClockDomain(
  points: readonly ProjectedTimelinePoint[],
  gaps: readonly { nextIndex: number }[],
  layout: TimelineLayout,
  requestedPointCount = points.length,
): TimelineClockDomain {
  const visiblePointCount = normalizedPointCount(points, requestedPointCount);
  if (visiblePointCount === 0) {
    return {
      data: [],
      ranges: [],
      pointBucketTimes: [],
      pointDataIndexes: [],
      rangeDataEnds: [],
      sourcePoints: [],
      visiblePointCount,
    };
  }

  const visiblePoints = visiblePointCount === points.length
    ? points
    : points.slice(0, visiblePointCount);
  const anchors = buildTimelineAxisAnchors(points, layout, visiblePointCount);
  const ranges = buildTimelineLogicalDayRanges(points, gaps, layout, visiblePointCount);
  if (anchors.length === 0 || ranges.length === 0) {
    return {
      data: [],
      ranges: [],
      pointBucketTimes: [],
      pointDataIndexes: [],
      rangeDataEnds: [],
      sourcePoints: visiblePoints,
      visiblePointCount,
    };
  }

  const data: TimelineClockSeriesData[] = [];
  const bucketStarts = new Map<number, number>();
  const rangeDataEnds: number[] = [];
  for (let dayIndex = 0; dayIndex < ranges.length; dayIndex += 1) {
    const range = ranges[dayIndex];
    const dataStart = data.length;
    bucketStarts.set(range.chartFrom, dataStart);
    for (let bucket = 0; bucket < range.bucketCount; bucket += 1) {
      data.push({
        time: (range.chartFrom + bucket * TIMELINE_CLOCK_BUCKET_SECONDS) as Time,
      });
    }
    rangeDataEnds.push(data.length);
  }

  const mutableBuckets = new Map<number, MutableBucket>();
  const pointBucketTimes = new Array<number>(visiblePointCount);
  const pointDataIndexes = new Array<number>(visiblePointCount).fill(-1);
  let dayIndex = 0;
  let previousDayIndex = -1;
  let segment = 0;
  const gapNextIndexes = new Set(
    gaps.map((gap) => gap.nextIndex).filter((index) => index > 0),
  );
  for (let pointIndex = 0; pointIndex < visiblePoints.length; pointIndex += 1) {
    const point = visiblePoints[pointIndex];
    while (dayIndex < ranges.length && point.time > ranges[dayIndex].chartTo) dayIndex += 1;
    const range = ranges[dayIndex];
    if (!range || point.time < range.chartFrom || point.time > range.chartTo) continue;
    if (
      pointIndex > 0
      && (previousDayIndex !== dayIndex || gapNextIndexes.has(pointIndex))
    ) {
      segment += 1;
    }
    previousDayIndex = dayIndex;

    const offset = Math.max(0, point.time - range.chartFrom);
    const bucketNumber = Math.min(
      range.bucketCount - 1,
      Math.floor(offset / TIMELINE_CLOCK_BUCKET_SECONDS),
    );
    const bucketTime = range.chartFrom + bucketNumber * TIMELINE_CLOCK_BUCKET_SECONDS;
    const dataIndex = (bucketStarts.get(range.chartFrom) ?? 0) + bucketNumber;
    let bucket = mutableBuckets.get(dataIndex);
    if (!bucket) {
      bucket = {
        time: bucketTime as Time,
        samples: [],
        high: point.value,
        low: point.value,
        last: point.value,
      };
      mutableBuckets.set(dataIndex, bucket);
    }
    bucket.samples.push({
      actualTime: point.actualTime,
      chartTime: point.time,
      value: point.value,
      segment,
      sourceIndex: pointIndex,
      offsetSeconds: Math.max(
        0,
        Math.min(TIMELINE_CLOCK_BUCKET_SECONDS, point.time - bucketTime),
      ),
    });
    bucket.high = Math.max(bucket.high, point.value);
    bucket.low = Math.min(bucket.low, point.value);
    bucket.last = point.value;
    pointBucketTimes[pointIndex] = bucketTime;
    pointDataIndexes[pointIndex] = dataIndex;
  }

  for (const [dataIndex, bucket] of mutableBuckets) data[dataIndex] = bucket;
  return {
    data,
    ranges,
    pointBucketTimes,
    pointDataIndexes,
    rangeDataEnds,
    sourcePoints: visiblePoints,
    visiblePointCount,
  };
}

function normalizedVisibleCount(domain: TimelineClockDomain, requestedPointCount: number): number {
  return normalizedPointCount(domain.sourcePoints, requestedPointCount);
}

export function timelineClockVisibleDataLength(
  domain: TimelineClockDomain,
  requestedPointCount: number,
): number {
  const pointCount = normalizedVisibleCount(domain, requestedPointCount);
  for (let pointIndex = pointCount - 1; pointIndex >= 0; pointIndex -= 1) {
    const dataIndex = domain.pointDataIndexes[pointIndex];
    if (dataIndex < 0) continue;
    return domain.rangeDataEnds.find((end) => dataIndex < end) ?? domain.data.length;
  }
  return 0;
}

export function timelineClockVisibleRanges(
  domain: TimelineClockDomain,
  requestedPointCount: number,
): TimelineLogicalDayRange[] {
  const dataLength = timelineClockVisibleDataLength(domain, requestedPointCount);
  const dayCount = dataLength === 0
    ? 0
    : domain.rangeDataEnds.findIndex((end) => dataLength <= end) + 1;
  return dayCount === domain.ranges.length
    ? domain.ranges
    : domain.ranges.slice(0, dayCount);
}

export function timelineClockDataItemAt(
  domain: TimelineClockDomain,
  dataIndex: number,
  requestedPointCount: number,
): TimelineClockSeriesData {
  const item = domain.data[dataIndex];
  if (!item || !isTimelineClockBucket(item)) return item ?? { time: 0 as Time };
  const pointCount = normalizedVisibleCount(domain, requestedPointCount);
  const first = item.samples[0]?.sourceIndex ?? Number.POSITIVE_INFINITY;
  const last = item.samples[item.samples.length - 1]?.sourceIndex ?? Number.NEGATIVE_INFINITY;
  if (last < pointCount) return item;
  if (first >= pointCount) return { time: item.time };
  const samples = item.samples.filter((sample) => sample.sourceIndex < pointCount);
  if (samples.length === 0) return { time: item.time };
  let high = samples[0].value;
  let low = samples[0].value;
  for (let index = 1; index < samples.length; index += 1) {
    high = Math.max(high, samples[index].value);
    low = Math.min(low, samples[index].value);
  }
  return {
    time: item.time,
    samples,
    high,
    low,
    last: samples[samples.length - 1].value,
  };
}

export function materializeTimelineClockData(
  domain: TimelineClockDomain,
  requestedPointCount: number,
): TimelineClockSeriesData[] {
  const dataLength = timelineClockVisibleDataLength(domain, requestedPointCount);
  return Array.from(
    { length: dataLength },
    (_, dataIndex) => timelineClockDataItemAt(domain, dataIndex, requestedPointCount),
  );
}

export function changedTimelineClockDataIndexes(
  domain: TimelineClockDomain,
  previousPointCount: number,
  nextPointCount: number,
): number[] {
  const previous = normalizedVisibleCount(domain, previousPointCount);
  const next = normalizedVisibleCount(domain, nextPointCount);
  const start = Math.min(previous, next);
  const end = Math.max(previous, next);
  const indexes = new Set<number>();
  for (let pointIndex = start; pointIndex < end; pointIndex += 1) {
    const dataIndex = domain.pointDataIndexes[pointIndex];
    if (dataIndex >= 0) indexes.add(dataIndex);
  }
  return [...indexes].sort((left, right) => left - right);
}

export function timelineClockDomainsSharePrefix(
  previous: TimelineClockDomain,
  next: TimelineClockDomain,
  pointCount: number,
): boolean {
  const count = Math.min(
    Math.max(0, Math.floor(pointCount)),
    previous.sourcePoints.length,
    next.sourcePoints.length,
  );
  if (count === 0) return true;
  const indexes = count === 1 ? [0] : [0, count - 1];
  return indexes.every((index) => {
    const left = previous.sourcePoints[index];
    const right = next.sourcePoints[index];
    return left.actualTime === right.actualTime
      && left.time === right.time
      && left.value === right.value
      && previous.pointDataIndexes[index] === next.pointDataIndexes[index];
  });
}

export function isTimelineClockBucket(
  value: TimelineClockSeriesData | null | undefined,
): value is TimelineClockBucket {
  return Boolean(value && "samples" in value && value.samples.length > 0);
}

export function nearestTimelineClockSample(
  bucket: TimelineClockBucket,
  targetChartTime: number,
): TimelineClockSample {
  let low = 0;
  let high = bucket.samples.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (bucket.samples[middle].chartTime < targetChartTime) low = middle + 1;
    else high = middle;
  }
  const right = bucket.samples[low];
  const left = bucket.samples[Math.max(0, low - 1)];
  return Math.abs(right.chartTime - targetChartTime) < Math.abs(targetChartTime - left.chartTime)
    ? right
    : left;
}

class TimelineClockRenderer implements ICustomSeriesPaneRenderer {
  private data: PaneRendererCustomData<Time, TimelineClockBucket> | null = null;
  private options: TimelineClockSeriesOptions | null = null;

  update(
    data: PaneRendererCustomData<Time, TimelineClockBucket>,
    options: TimelineClockSeriesOptions,
  ): void {
    this.data = data;
    this.options = options;
  }

  draw(target: DrawTarget, priceConverter: PriceConverter): void {
    const data = this.data;
    const options = this.options;
    const visibleRange = data?.visibleRange;
    if (!data || !options || !visibleRange || data.bars.length === 0) return;

    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const from = Math.max(0, Math.floor(visibleRange.from) - 1);
      const to = Math.min(data.bars.length, Math.ceil(visibleRange.to) + 1);
      const gradient = context.createLinearGradient(0, 0, 0, mediaSize.height);
      gradient.addColorStop(0, options.topColor);
      gradient.addColorStop(1, options.bottomColor);
      context.fillStyle = gradient;
      context.strokeStyle = options.lineColor;
      context.lineWidth = options.lineWidth;
      context.lineCap = "round";
      context.lineJoin = "round";

      const drawPaths = (fillArea: boolean): Array<[number, number]> => {
        const isolated: Array<[number, number]> = [];
        let pointCount = 0;
        let firstX = 0;
        let firstY = 0;
        let lastX = 0;
        let previousSegment = -1;
        let previousLogicalIndex = Number.NaN;
        const finishSegment = () => {
          if (pointCount >= 2 && fillArea) {
            context.lineTo(lastX, mediaSize.height);
            context.lineTo(firstX, mediaSize.height);
            context.closePath();
          } else if (pointCount === 1 && !fillArea) {
            isolated.push([firstX, firstY]);
          }
          pointCount = 0;
        };

        context.beginPath();
        for (let index = from; index < to; index += 1) {
          const bar = data.bars[index];
          for (const sample of bar.originalData.samples) {
            const coordinate = priceConverter(sample.value);
            if (coordinate === null || !Number.isFinite(coordinate)) continue;
            const x = bar.x
              + sample.offsetSeconds / TIMELINE_CLOCK_BUCKET_SECONDS * data.barSpacing;
            const y = Number(coordinate);
            const continuous = pointCount > 0
              && previousSegment === sample.segment
              && timelineClockBucketTimesAreAdjacent(previousLogicalIndex, Number(bar.time));
            if (!continuous) {
              finishSegment();
              firstX = x;
              firstY = y;
              lastX = x;
              pointCount = 1;
            } else {
              if (pointCount === 1) context.moveTo(firstX, firstY);
              context.lineTo(x, y);
              lastX = x;
              pointCount += 1;
            }
            previousSegment = sample.segment;
            previousLogicalIndex = Number(bar.time);
          }
        }
        finishSegment();
        if (fillArea) context.fill();
        else context.stroke();
        return isolated;
      };

      drawPaths(true);
      const isolated = drawPaths(false);
      if (isolated.length > 0) {
        context.fillStyle = options.lineColor;
        context.beginPath();
        for (const [x, y] of isolated) {
          context.moveTo(x + Math.max(1, options.lineWidth / 2), y);
          context.arc(x, y, Math.max(1, options.lineWidth / 2), 0, Math.PI * 2);
        }
        context.fill();
      }
    });
  }
}

export class TimelineClockSeries implements ICustomSeriesPaneView<
  Time,
  TimelineClockBucket,
  TimelineClockSeriesOptions
> {
  private readonly paneRenderer = new TimelineClockRenderer();

  renderer(): ICustomSeriesPaneRenderer {
    return this.paneRenderer;
  }

  update(
    data: PaneRendererCustomData<Time, TimelineClockBucket>,
    options: TimelineClockSeriesOptions,
  ): void {
    this.paneRenderer.update(data, options);
  }

  priceValueBuilder(data: TimelineClockBucket): [number, number, number] {
    return [data.high, data.low, data.last];
  }

  isWhitespace(
    data: TimelineClockBucket | WhitespaceData<Time>,
  ): data is WhitespaceData<Time> {
    return !("samples" in data) || data.samples.length === 0;
  }

  defaultOptions(): TimelineClockSeriesOptions {
    return {
      ...customSeriesDefaultOptions,
      color: "#4e7deb",
      lineColor: "#4e7deb",
      lineWidth: 2,
      topColor: "rgba(78, 125, 235, .20)",
      bottomColor: "rgba(78, 125, 235, .015)",
      priceLineVisible: false,
      lastValueVisible: false,
    };
  }
}
