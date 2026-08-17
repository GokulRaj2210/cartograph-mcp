/** TS client: interfaces, implements clauses, and cross-file imports. */

import { padLabel, slugify, DEFAULT_LOCALE } from "./util";

/** The contract a transport must satisfy. */
export interface Transport {
  send(body: string): Promise<string>;
}

export type Options = {
  retries: number;
};

export enum Mode {
  Fast,
  Safe,
}

/** Implements Transport -> an `inherits` edge. */
export class HttpTransport implements Transport {
  constructor(private readonly base: string) {}

  async send(body: string): Promise<string> {
    const slug = slugify(body);
    return padLabel(slug, 10);
  }
}

export const makeClient = (opts: Options): HttpTransport => {
  return new HttpTransport(DEFAULT_LOCALE);
};
